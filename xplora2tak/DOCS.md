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
  protocol: tcp              # tcp | tls | udp (must match the port!)
  cot_type: a-f-G-U-C
  stale_seconds: 900
  callsign_prefix: ""        # e.g. "KID-" -> callsign "KID-Emma"
  contact_presence: true     # show in ATAK/WinTAK contacts list
  team_color: Cyan
  team_role: Team Member
  tls_p12_file: /ssl/tak/client.p12    # easiest: the .p12 from TAK Server
  tls_p12_password: atakatak
  tls_ca_file: /ssl/tak/ca.pem         # or PEM files instead of .p12:
  tls_cert_file: /ssl/tak/client.pem
  tls_key_file: /ssl/tak/client.key
  tls_verify: true
  tls_check_hostname: false  # TAK certs rarely match the public hostname
log_level: info
```

### Authentication

Use the same credentials as the Xplora smartphone app. **Use `email` +
`password`** — the current Xplora API no longer appears to accept phone
number logins. The legacy phone path (`country_code`, e.g. `1` for the US —
no `+`, plus `phone_number`) is kept as a fallback but may fail. If both
are given, email wins.

The auth token is cached in the add-on's private `/data` directory and
survives restarts, so restarting the add-on does not trigger a new login.

### API overrides (advanced)

The add-on ships with the API endpoint and key pair known to work against
the current (2026) Xplora backend. If Xplora moves the endpoint or rotates
keys again, they can be overridden without a new release:

```yaml
api:
  endpoint: https://api.prod.myxplora.com/api
  key: ""
  secret: ""
```

Leave all three empty to use the built-in defaults.

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
- `tls` — TLS input (default port 8089; iTAK/ATAK call this "SSL" — same
  thing). TAK Server requires a **client certificate** on this port.
  Copy your certificate files into Home Assistant's **`ssl` folder** (via
  the Samba add-on, File editor, or SSH) and reference them as
  `/ssl/<filename>` — the add-on can only see `/ssl` and `/share`, so a
  path anywhere else fails with "file not found".

  The easiest route is the `.p12` bundle from your TAK Server (from
  certificate enrollment or a data package) — no conversion needed:

  ```yaml
  tak:
    protocol: tls
    port: 8089
    tls_p12_file: /ssl/tak/xplora-client.p12
    tls_p12_password: atakatak        # the default for TAK Server bundles
    tls_ca_file: /ssl/tak/truststore-root.pem   # or set tls_verify: false
  ```

  Alternatively use PEM files directly via `tls_cert_file` +
  `tls_key_file`. If your server's certificate is self-signed and you
  don't have the CA file, set `tls_verify: false`.

  **Hostname checking is off by default** (`tls_check_hostname: false`):
  TAK Server certificates are issued by its own CA for an internal name
  like `takserver`, which never matches the public hostname or DDNS name
  you dial — ATAK/iTAK behave the same way and only verify the CA chain.
  Set `tls_check_hostname: true` only if your server certificate really
  contains the hostname you connect to.
- `udp` — one datagram per event (e.g. multicast-style inputs; unicast only).

**Protocol and port must match.** TAK Server drops mismatched traffic
*silently* — the add-on can log "Sent" while the server discards every
byte. Defaults are:

| Input           | `protocol` | `port` | Needs certs |
|-----------------|-----------|--------|-------------|
| Streaming TCP   | `tcp`     | 8087   | no          |
| Streaming TLS   | `tls`     | 8089   | yes (client cert + CA) |

The add-on warns at startup if it sees `tcp`+8089 or `tls`+8087.

Each watch becomes a CoT event with `uid=XPLORA-<watch-id>`, the watch name
as callsign, the location accuracy as circular error (`ce`), and battery in
the `<status>` element. Events are re-sent every poll so markers stay fresh;
`stale_seconds` controls when a marker grays out if updates stop.

With `contact_presence: true` (the default) the event is shaped like a TAK
client's own position report (PLI), so the watch shows up in ATAK/WinTAK's
**contacts list** as a trackable team member — with `team_color` (White,
Yellow, Orange, Magenta, Red, Maroon, Purple, Dark Blue, Blue, Cyan, Teal,
Green, Dark Green, Brown) and `team_role`. Set `contact_presence: false`
for a plain map marker instead (uses `cot_type`, default `a-f-G-U-C`).

### Node-RED

If you prefer to keep your existing Node-RED → TAK flow, leave `tak.enabled:
false` and consume the tracker entity in Node-RED, e.g. with a
`server-state-changed` node watching `device_tracker.xplora_<name>`; latitude
and longitude are in `msg.data.attributes`.

## Updating

New versions land on the repository's default branch; Home Assistant
re-reads it periodically. To pull an update immediately: **Settings →
Add-ons → Add-on store → ⋮ (top right) → Check for updates**, then press
*Update* on the add-on page. Updating **preserves your configuration** —
never uninstall/reinstall to update, as uninstalling wipes the saved
options and the cached Xplora token.

## Troubleshooting

- **"Sign-in failed" / "Authentication failed."** — the API rejected the
  credentials. Check, in order:
  1. The exact same email/phone + password works in the Xplora phone app.
  2. Accounts created with **Google or Apple sign-in have no password** and
     cannot authenticate here — set a password in the Xplora app first.
  3. Use **email login** — the current API no longer appears to accept
     phone-number sign-ins. If your account has no email, add one in the
     Xplora app.
- **"Sign-in postponed for Ns"** — not an error: the add-on refuses to
  sign in more than once per 15 minutes to protect your IP. It waits the
  shown time and retries automatically; happens after quick restarts.
- **"Refusing to sign in again for another Ns"** — the login rate-limiter.
  Wait; it protects your IP.
- **HTTP 403** — possible IP block. Stop the add-on for at least 24 h.
  Consider whether other Xplora clients share your IP.
- **No entities in HA** — check the Mosquitto add-on is running and that the
  MQTT integration is set up in Home Assistant (Settings → Devices &
  Services).
- **TLS connects but nothing appears in TAK** — two usual causes:
  1. *Client certificate rejected after the handshake* (TLS 1.3 reports
     this only as a disconnect). The add-on detects this and logs
     "TAK server closed the connection ... client certificate was not
     accepted" — fix by using a client cert issued by the same TAK server
     (enrollment or `makeCert.sh client`), not a cert from elsewhere.
  2. *Group filtering*: TAK Server only forwards events between users
     that share a group. In the server's admin UI check that the
     add-on's certificate user and your ATAK/iTAK user are in a common
     group (e.g. both `__ANON__`). The server's
     `takserver-messaging.log` shows both problems explicitly.
