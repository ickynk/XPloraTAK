"""MQTT output with Home Assistant discovery.

Creates one device per watch containing:
  * a ``device_tracker`` entity (GPS source, lat/lon/accuracy attributes) -
    ready to feed a Node-RED -> TAK flow,
  * a battery ``sensor`` entity.

The broker is auto-discovered through the Supervisor's MQTT service (the
Mosquitto add-on) unless a host is configured explicitly.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import paho.mqtt.client as mqtt
import requests

_LOGGER = logging.getLogger(__name__)

AVAILABILITY_TOPIC = "xplora2tak/status"


def _supervisor_mqtt_service() -> Optional[dict[str, Any]]:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        resp = requests.get(
            "http://supervisor/services/mqtt",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("data")
        _LOGGER.debug("Supervisor MQTT service query returned %s", resp.status_code)
    except requests.RequestException as exc:
        _LOGGER.debug("Supervisor MQTT service query failed: %s", exc)
    return None


class MqttPublisher:
    def __init__(self, options: dict[str, Any]) -> None:
        host = options.get("host") or ""
        port = int(options.get("port") or 1883)
        username = options.get("username") or ""
        password = options.get("password") or ""

        if not host:
            service = _supervisor_mqtt_service()
            if service:
                host = service.get("host", "core-mosquitto")
                port = int(service.get("port", 1883))
                username = service.get("username") or username
                password = service.get("password") or password
                _LOGGER.info("Using Supervisor-provided MQTT broker at %s:%d", host, port)
            else:
                raise ValueError(
                    "No MQTT host configured and no Supervisor MQTT service found. "
                    "Install/start the Mosquitto broker add-on or set mqtt.host."
                )

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="xplora2tak"
        )
        if username:
            self._client.username_pw_set(username, password)
        self._client.will_set(AVAILABILITY_TOPIC, "offline", retain=True)
        self._client.connect(host, port, keepalive=120)
        self._client.loop_start()
        self._client.publish(AVAILABILITY_TOPIC, "online", retain=True)
        self._discovered: set[str] = set()

    def close(self) -> None:
        try:
            self._client.publish(AVAILABILITY_TOPIC, "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001 - best effort on shutdown
            pass

    # ------------------------------------------------------------- discovery

    def announce_watch(self, wuid: str, name: str) -> None:
        if wuid in self._discovered:
            return
        slug = _slug(wuid)
        device = {
            "identifiers": [f"xplora2tak_{slug}"],
            "name": f"Xplora {name}",
            "manufacturer": "Xplora",
            "model": "Xplora watch (via xplora2tak)",
        }
        tracker_cfg = {
            "name": None,
            "unique_id": f"xplora2tak_{slug}_tracker",
            "state_topic": f"xplora2tak/{slug}/state",
            "json_attributes_topic": f"xplora2tak/{slug}/attributes",
            "source_type": "gps",
            "availability_topic": AVAILABILITY_TOPIC,
            "device": device,
        }
        battery_cfg = {
            "name": "Battery",
            "unique_id": f"xplora2tak_{slug}_battery",
            "state_topic": f"xplora2tak/{slug}/battery",
            "device_class": "battery",
            "unit_of_measurement": "%",
            "availability_topic": AVAILABILITY_TOPIC,
            "device": device,
        }
        self._client.publish(
            f"homeassistant/device_tracker/xplora2tak_{slug}/config",
            json.dumps(tracker_cfg),
            retain=True,
        )
        self._client.publish(
            f"homeassistant/sensor/xplora2tak_{slug}_battery/config",
            json.dumps(battery_cfg),
            retain=True,
        )
        self._discovered.add(wuid)
        _LOGGER.info("Announced watch '%s' (%s) via MQTT discovery", name, wuid)

    # ----------------------------------------------------------------- state

    def publish_location(self, wuid: str, attributes: dict[str, Any]) -> None:
        slug = _slug(wuid)
        self._client.publish(
            f"xplora2tak/{slug}/attributes", json.dumps(attributes), retain=True
        )
        # With latitude/longitude present in the attributes topic, Home
        # Assistant derives the zone itself; the state payload is a fallback.
        self._client.publish(f"xplora2tak/{slug}/state", "not_home", retain=True)
        battery = attributes.get("battery_level")
        if battery is not None:
            self._client.publish(f"xplora2tak/{slug}/battery", str(battery), retain=True)


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value).lower()
