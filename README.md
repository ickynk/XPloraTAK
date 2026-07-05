# XPloraTAK

Home Assistant add-on repository that bridges **Xplora® kids' smartwatch
locations** into Home Assistant (via MQTT `device_tracker` entities) and/or
straight into a **TAK server** as Cursor-on-Target (CoT) events.

[![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fickynk%2FXPloraTAK)

## Why this exists

The well-known [Ludy87/xplora_watch](https://github.com/Ludy87/xplora_watch)
custom integration was discontinued and archived after **Xplora started
IP-banning clients** of the reverse-engineered API
([issue #498](https://github.com/Ludy87/xplora_watch/issues/498), confirmed by
the Xplora team in March 2025). The API itself
(`https://api.myxplora.com/api`, GraphQL) is still online — the problem was
client behavior: the old integration re-authenticated roughly every four
minutes and polled aggressively.

This add-on is a fresh, minimal implementation designed to be a *polite*
client:

- **Passive reads only** — it queries `watchLastLocate` (the position the
  watch last uploaded on its own schedule) and **never** sends locate
  commands that ping the watch.
- **Rare logins** — the auth token is cached on disk and reused until the API
  rejects it; sign-ins are hard-limited to at most once per 15 minutes.
- **Conservative polling** — minimum interval 120 s (default 300 s) with
  random jitter, exponential backoff on errors, and a 6-hour stand-off if the
  API ever returns HTTP 403.

> ⚠️ This still uses an **unofficial** API. Xplora has banned users of
> similar tools before. Keep the poll interval high (300 s or more) and
> understand that access could stop working at any time. Use at your own
> risk.

## What you get

For every watch on your Xplora account:

- `device_tracker.xplora_<name>` — GPS tracker entity with `latitude`,
  `longitude`, `gps_accuracy`, `battery_level`, address, safe-zone and
  charging attributes (via MQTT discovery; requires the Mosquitto broker
  add-on or any MQTT broker).
- `sensor.xplora_<name>_battery` — battery percentage.
- Optionally, **CoT events sent directly to your TAK server** over TCP
  (8087), TLS (8089, with client certificates) or UDP — no Node-RED needed.

## Install

1. In Home Assistant: **Settings → Add-ons → Add-on store → ⋮ → Repositories**
   and add `https://github.com/ickynk/XPloraTAK`.
2. Install **Xplora → MQTT / TAK Bridge**.
3. Configure your Xplora credentials (email *or* country code + phone
   number, plus password — the same ones you use in the Xplora app).
4. Make sure the **Mosquitto broker** add-on is running (the bridge finds it
   automatically), or point `mqtt.host` at your own broker.
5. Start the add-on and watch the log.

See [xplora2tak/DOCS.md](xplora2tak/DOCS.md) for every option, TAK/TLS setup
and Node-RED wiring notes.

## Feeding your existing Node-RED → TAK flow

The tracker entities behave like any other GPS `device_tracker`. In your
existing flow, listen for state changes of `device_tracker.xplora_<name>`
(e.g. with a `server-state-changed` node from
node-red-contrib-home-assistant-websocket), read
`data.attributes.latitude/longitude`, and pass that to your CoT/TAK node
exactly as you do today.

Alternatively, enable the `tak` section in the add-on configuration and skip
Node-RED entirely — the add-on builds and delivers the CoT itself.

## Disclaimer

Not affiliated with, endorsed by, or supported by Xplora Technologies AS.
Xplora® is a trademark of its owner. Protocol details derive from the
MIT-licensed [pyxplora_api](https://github.com/Ludy87/pyxplora_api) project.
