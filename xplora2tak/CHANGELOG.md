# Changelog

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
