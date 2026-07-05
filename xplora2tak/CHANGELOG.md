# Changelog

## 1.4.1

- All configuration keys are now optional in the add-on schema. Previous
  releases added required keys, which made the Supervisor reject options
  saved by an older version and forced re-entering the configuration
  after updates. From this version on, saved options survive every
  update; the app applies sensible defaults for anything unset.

## 1.4.0

- **Persistent TAK connection with acceptance detection.** The sender now
  holds one long-lived connection like a real TAK client instead of
  connecting per batch. This makes the watch a proper online contact and,
  critically, detects the failure mode where a TLS 1.3 handshake succeeds
  but the server rejects the client certificate afterwards and silently
  discards everything: that now logs "TAK server closed the connection
  ... client certificate was not accepted" instead of a false "Sent".
- Inbound traffic pushed by the server is drained each cycle (debug log
  "connection accepted and healthy") and one automatic reconnect is
  attempted per send.

## 1.3.1

- Fix "certificate verify failed: Hostname mismatch" when connecting to a
  TAK server through a hostname its certificate wasn't issued for (the
  normal case with TAK's internal CA). The CA chain is still fully
  verified, but the hostname comparison is now skipped by default —
  matching ATAK/iTAK behavior. Re-enable strict matching with
  `tls_check_hostname: true`.
- TLS verification failures now log an actionable hint (hostname mismatch
  vs. untrusted CA) instead of the raw OpenSSL error alone.

## 1.3.0

- **Native PKCS#12 support**: point `tls_p12_file` (+ `tls_p12_password`)
  at the `.p12` bundle from TAK Server enrollment or a data package — no
  more manual openssl conversion. The CA chain inside the bundle is used
  automatically when `tls_ca_file` is not set.
- Certificate paths are validated at startup: a missing file now fails
  with a message naming the option and path, and a reminder that the
  add-on can only see `/ssl` and `/share` (previously a bare
  "[Errno 2] No such file or directory" at send time).
- Clear error for a wrong `.p12` password, mentioning the usual TAK
  default ("atakatak").

## 1.2.0

- **Watches now appear in ATAK/WinTAK's contacts list.** With the new
  `contact_presence: true` default, CoT events are shaped like a TAK
  client's own position report (PLI: `takv`, routable contact endpoint,
  `uid Droid`, `__group`), so each watch is a trackable team member with
  configurable `team_color` and `team_role`. Set `contact_presence: false`
  for the old plain-marker behavior.
- Warn at startup on the classic protocol/port mismatch (`tcp` with port
  8089 or `tls` with 8087) — TAK Server silently discards mismatched
  traffic, which looks like "sent but nothing on the map".
- TLS connections now finish with a proper close_notify so the server
  flushes events before the connection drops; TLS handshake details are
  logged at debug level.

## 1.1.0

- **Fix sign-in against the current (2026) Xplora API.** The protocol
  changed since the archived pyxplora_api era; verified against clients
  working today:
  - endpoint moved to `https://api.prod.myxplora.com/api`,
  - updated API key/secret pair,
  - email sign-ins use the `WEB` client type with no phone variables,
  - subsequent requests are signed with the account's `w360` secret when
    present.
- Phone-number login kept as a legacy fallback with a warning (the current
  API reportedly accepts email logins only).
- New optional `api:` config section to override endpoint/key/secret
  without a new release.
- Restarting the add-on shortly after a failed sign-in now logs a calm
  "Sign-in postponed for Ns" info message and waits exactly that long,
  instead of an error with a fixed backoff.
- GraphQL error responses are logged verbatim at debug level.

## 1.0.2

- Lowercase the configured email address before sign-in. The Xplora API
  matches emails exactly against the (lowercased) stored value, so a
  mixed-case email like `Name@gmail.com` failed with "Authentication
  failed." even though the phone app accepted it.

## 1.0.1

- Fix "Authentication failed." on sign-in: unused login fields are now sent
  with the same semantics as the reference client (`emailAddress` as JSON
  null instead of an empty string), which could previously push the server
  onto the wrong authentication path.
- Normalize credentials: strip whitespace, remove a leading '+' from the
  country code and spaces from the phone number.
- Clearer sign-in error messages (Google/Apple SSO accounts, country code
  format, email vs phone login).

## 1.0.0

- Initial release.
- Passive polling of Xplora `watchLastLocate` (no watch pings).
- On-disk token cache; sign-ins limited to at most once per 15 minutes.
- MQTT output with Home Assistant discovery: `device_tracker` + battery
  `sensor` per watch.
- Optional direct CoT delivery to a TAK server via TCP, TLS (with client
  certificates) or UDP.
- Conservative error handling: exponential backoff, 6-hour stand-off on
  HTTP 403.
