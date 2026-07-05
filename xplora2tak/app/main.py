"""xplora2tak - poll Xplora watch locations, publish to MQTT and/or TAK."""

from __future__ import annotations

import json
import logging
import random
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from cot import TakSender, build_cot
from mqtt_out import MqttPublisher
from xplora import (
    XploraAuthError,
    XploraBlockedError,
    XploraClient,
    XploraError,
)

_LOGGER = logging.getLogger("xplora2tak")

OPTIONS_FILE = "/data/options.json"

# Backoff schedule (seconds) applied on consecutive failures.
BACKOFF_STEPS = [300, 600, 1800, 3600]
BLOCKED_BACKOFF = 6 * 3600  # HTTP 403: stand well back.

_shutdown = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _shutdown
    _LOGGER.info("Received signal %d, shutting down", signum)
    _shutdown = True


def load_options() -> dict[str, Any]:
    with open(OPTIONS_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def sleep_interruptible(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _shutdown and time.monotonic() < end:
        time.sleep(min(5, max(0, end - time.monotonic())))


def location_attributes(loc: dict[str, Any]) -> Optional[dict[str, Any]]:
    lat, lng = loc.get("lat"), loc.get("lng")
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    fix_epoch = loc.get("tm")
    fix_iso = None
    try:
        num = float(fix_epoch)
        if num > 1e12:
            num /= 1000.0
        fix_iso = datetime.fromtimestamp(num, timezone.utc).isoformat()
    except (TypeError, ValueError):
        num = None
    attrs: dict[str, Any] = {
        "latitude": lat,
        "longitude": lng,
        "source_type": "gps",
    }
    if loc.get("rad") is not None:
        try:
            attrs["gps_accuracy"] = int(float(loc["rad"]))
        except (TypeError, ValueError):
            pass
    if loc.get("battery") is not None:
        attrs["battery_level"] = loc["battery"]
    for key, attr in (
        ("locateType", "locate_type"),
        ("addr", "address"),
        ("poi", "poi"),
        ("city", "city"),
        ("isCharging", "is_charging"),
        ("isInSafeZone", "in_safe_zone"),
        ("safeZoneLabel", "safe_zone_label"),
    ):
        if loc.get(key) not in (None, ""):
            attrs[attr] = loc[key]
    if fix_iso:
        attrs["last_fix"] = fix_iso
        attrs["_fix_epoch"] = num
    return attrs


def main() -> int:
    options = load_options()
    logging.basicConfig(
        level=getattr(logging, str(options.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    auth = options.get("auth") or {}
    try:
        client = XploraClient(
            password=auth.get("password") or "",
            email=auth.get("email") or "",
            country_code=auth.get("country_code") or "",
            phone_number=auth.get("phone_number") or "",
            user_lang=options.get("user_lang") or "en-US",
            timezone_name=options.get("timezone") or "UTC",
        )
    except XploraAuthError as exc:
        _LOGGER.error("%s", exc)
        return 1

    mqtt_opts = options.get("mqtt") or {}
    tak_opts = options.get("tak") or {}
    mqtt_enabled = bool(mqtt_opts.get("enabled", True))
    tak_enabled = bool(tak_opts.get("enabled", False))
    if not mqtt_enabled and not tak_enabled:
        _LOGGER.error("Both MQTT and TAK outputs are disabled - nothing to do")
        return 1

    publisher: Optional[MqttPublisher] = None
    if mqtt_enabled:
        try:
            publisher = MqttPublisher(mqtt_opts)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("MQTT setup failed: %s", exc)
            return 1

    sender: Optional[TakSender] = None
    if tak_enabled:
        try:
            sender = TakSender(tak_opts)
        except ValueError as exc:
            _LOGGER.error("%s", exc)
            return 1

    poll_interval = max(120, int(options.get("poll_interval", 300)))
    watch_filter = {w.strip() for w in (options.get("watch_ids") or []) if w.strip()}
    cot_type = tak_opts.get("cot_type") or "a-f-G-U-C"
    stale_seconds = int(tak_opts.get("stale_seconds") or 900)
    callsign_prefix = tak_opts.get("callsign_prefix") or ""

    _LOGGER.info(
        "Polling every %ds (passive reads only). Outputs: mqtt=%s tak=%s",
        poll_interval,
        mqtt_enabled,
        tak_enabled,
    )

    watches: list[dict[str, Any]] = []
    last_fix: dict[str, float] = {}
    failures = 0

    while not _shutdown:
        try:
            if not watches:
                watches = client.get_watches()
                if watch_filter:
                    watches = [w for w in watches if w["id"] in watch_filter]
                if not watches:
                    _LOGGER.warning(
                        "No watches found on this account%s",
                        " matching watch_ids filter" if watch_filter else "",
                    )
                else:
                    _LOGGER.info(
                        "Tracking %d watch(es): %s",
                        len(watches),
                        ", ".join(f"{w['name']} ({w['id']})" for w in watches),
                    )

            cot_events: list[bytes] = []
            for watch in watches:
                loc = client.get_last_location(watch["id"])
                if not loc:
                    _LOGGER.debug("No location for %s yet", watch["name"])
                    continue
                attrs = location_attributes(loc)
                if attrs is None:
                    _LOGGER.debug("Location without coordinates for %s", watch["name"])
                    continue

                fix_epoch = attrs.pop("_fix_epoch", None)
                is_new = fix_epoch is None or fix_epoch != last_fix.get(watch["id"])
                if fix_epoch is not None:
                    last_fix[watch["id"]] = fix_epoch

                if publisher:
                    publisher.announce_watch(watch["id"], watch["name"])
                    publisher.publish_location(watch["id"], attrs)

                if sender:
                    # Re-send even unchanged fixes so the marker never goes
                    # stale on the TAK side, but log only new ones.
                    fix_dt = (
                        datetime.fromtimestamp(fix_epoch, timezone.utc)
                        if fix_epoch
                        else None
                    )
                    remarks_bits = []
                    if attrs.get("address"):
                        remarks_bits.append(str(attrs["address"]))
                    if attrs.get("locate_type"):
                        remarks_bits.append(f"fix: {attrs['locate_type']}")
                    cot_events.append(
                        build_cot(
                            uid=f"XPLORA-{watch['id']}",
                            callsign=f"{callsign_prefix}{watch['name']}",
                            lat=attrs["latitude"],
                            lon=attrs["longitude"],
                            cot_type=cot_type,
                            accuracy_m=attrs.get("gps_accuracy"),
                            battery=attrs.get("battery_level"),
                            fix_time=fix_dt,
                            stale_seconds=stale_seconds,
                            remarks=" | ".join(remarks_bits),
                        )
                    )
                if is_new:
                    _LOGGER.info(
                        "%s: %.6f, %.6f (±%sm, battery %s%%)",
                        watch["name"],
                        attrs["latitude"],
                        attrs["longitude"],
                        attrs.get("gps_accuracy", "?"),
                        attrs.get("battery_level", "?"),
                    )

            if sender and cot_events:
                try:
                    sender.send(cot_events)
                except OSError as exc:
                    _LOGGER.warning("Could not deliver CoT to TAK server: %s", exc)

            failures = 0
            delay = poll_interval * random.uniform(1.0, 1.15)

        except XploraBlockedError as exc:
            _LOGGER.error("%s", exc)
            delay = BLOCKED_BACKOFF
        except XploraAuthError as exc:
            failures += 1
            _LOGGER.error("Authentication problem: %s", exc)
            delay = BACKOFF_STEPS[min(failures, len(BACKOFF_STEPS)) - 1]
        except XploraError as exc:
            failures += 1
            _LOGGER.warning("Xplora API error (attempt %d): %s", failures, exc)
            delay = BACKOFF_STEPS[min(failures, len(BACKOFF_STEPS)) - 1]

        sleep_interruptible(delay)

    if publisher:
        publisher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
