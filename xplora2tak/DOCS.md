# Xplora → MQTT / TAK Bridge

Polls the Xplora cloud for the last reported position of every watch on your
account and publishes it:

1. **MQTT** (default): creates `device_tracker` + battery `sensor` entities
   in Home Assistant via MQTT discovery. These feed dashboards, automations,
   and your Node-RED → TAK flow.
2. **TAK** (optional): builds Cursor-on-Target (CoT) events and sends them
   directly to a TAK server over TCP, TLS or UDP.

## ⚠️ Read this first — ban risk

This add-on talks to the same **unofficial** Xplora API that got the old
`xplora_watch` integration killed: Xplora confirmed IP-banning users of that
plugin in 2025. This bridge is deliberately gentle (passive reads only,
cached tokens, ≥120 s polling, backoff on errors), but the risk is not zero.

Recommendations:

- Keep `poll_interval` at **300 s or higher**.
- Don't run other Xplora API clients from the same IP at the same time.
- If the log ever shows `HTTP 403 ... may indicate an IP block`, **stop the
  add-on for a day**. The add-on itself backs off for 6 hours automatically.

## Configuration

```yaml
auth:
  email: you@example.com     # EITHER email...
  country_code: ""           # ...OR country code + phone number
  phone_number: ""           # (the login you use in the Xplora app)
  password: your-password
user_lang: en-US
timezone: America/New_York   # IANA timezone of your account
poll_interval: 300           # seconds between polls (min 120)
watch_ids: []                # optional: only track these watch ids (wuid)
mqtt:
  enabled: true
  host: ""                   # empty = auto-discover the Mosquitto add-on
  port: 1883
  username: ""
  password: ""
tak:
  enabled: false
  host: tak.example.com
  port: 8087
  protocol: tcp              # tcp | tls | udp
  cot_type: a-f-G-U-C
  stale_seconds: 900
  callsign_prefix: ""        # e.g. "KID-" -> callsign "KID-Emma"
  tls_ca_file: /ssl/tak/ca.pem
  tls_cert_file: /ssl/tak/client.pem   # client cert (PEM) if your server requires it
  tls_key_file: /ssl/tak/client.key
  tls_verify: true
log_level: info
```

### Authentication

Use the same credentials as the Xplora smartphone app. Either fill in
`email`, or `country_code` (e.g. `1` for the US, `49` for Germany — no `+`)
plus `phone_number`. If both are given, email wins.

The auth token is cached in the add-on's private `/data` directory and
survives restarts, so restarting the add-on does not trigger a new login.

### Watches

All watches (children) on the account are tracked by default. To restrict
this, start the add-on once, copy the watch ids from the log line
`Tracking N watch(es): Name (id), ...`, and list them under `watch_ids`.

### MQTT output

With the Mosquitto broker add-on installed, leave `mqtt.host` empty — the
broker and credentials are discovered through the Supervisor. For an
external broker, set host/port/username/password explicitly.

Entities appear automatically (MQTT discovery):

- `device_tracker.xplora_<name>` — state `home`/`not_home`/zone, with
  attributes `latitude`, `longitude`, `gps_accuracy`, `battery_level`,
  `address`, `locate_type` (GPS/WIFI/CELL), `is_charging`, `in_safe_zone`,
  `last_fix`.
- `sensor.xplora_<name> Battery` — battery percentage.

### TAK output

Set `tak.enabled: true` and point `host`/`port` at your TAK server:

- `tcp` — plain CoT streaming input (TAK Server default port 8087).
- `tls` — TLS input (default port 8089). Put your certificates in
  Home Assistant's `/ssl` directory and reference them as `/ssl/...`.
  PKCS#12 (`.p12`) files must be converted to PEM first:
  `openssl pkcs12 -in client.p12 -out client.pem -clcerts -nodes`.
- `udp` — one datagram per event (e.g. multicast-style inputs; unicast only).

Each watch becomes a CoT event with `uid=XPLORA-<watch-id>`, the watch name
as callsign, the location accuracy as circular error (`ce`), and battery in
the `<status>` element. Events are re-sent every poll so markers stay fresh;
`stale_seconds` controls when a marker grays out if updates stop.

`cot_type` defaults to `a-f-G-U-C` (friendly ground unit). Change it to
whatever your TAK deployment expects.

### Node-RED

If you prefer to keep your existing Node-RED → TAK flow, leave `tak.enabled:
false` and consume the tracker entity in Node-RED, e.g. with a
`server-state-changed` node watching `device_tracker.xplora_<name>`; latitude
and longitude are in `msg.data.attributes`.

## Troubleshooting

- **"Sign-in failed" / "Authentication failed."** — the API rejected the
  credentials. Check, in order:
  1. The exact same email/phone + password works in the Xplora phone app.
  2. Accounts created with **Google or Apple sign-in have no password** and
     cannot authenticate here — set a password in the Xplora app first.
  3. `country_code` must be digits only, no `+` (e.g. `1`, `44`, `49`).
  4. If you registered with a phone number, log in with
     `country_code` + `phone_number` and leave `email` empty (and vice
     versa).
- **"Refusing to sign in again for another Ns"** — the login rate-limiter.
  Wait; it protects your IP.
- **HTTP 403** — possible IP block. Stop the add-on for at least 24 h.
  Consider whether other Xplora clients share your IP.
- **No entities in HA** — check the Mosquitto add-on is running and that the
  MQTT integration is set up in Home Assistant (Settings → Devices &
  Services).
