# Changelog

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
