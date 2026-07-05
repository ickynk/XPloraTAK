"""Cursor-on-Target (CoT) generation and delivery to a TAK server.

Builds standard CoT 2.0 event XML from an Xplora location fix and sends it
over TCP (default, TAK port 8087), TLS (TAK port 8089, with optional client
certificate) or UDP.
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from xml.sax.saxutils import escape, quoteattr

_LOGGER = logging.getLogger(__name__)

COT_TIME_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
UNKNOWN = "9999999.0"


def build_cot(
    uid: str,
    callsign: str,
    lat: float,
    lon: float,
    cot_type: str = "a-f-G-U-C",
    accuracy_m: Optional[float] = None,
    battery: Optional[int] = None,
    fix_time: Optional[datetime] = None,
    stale_seconds: int = 900,
    remarks: str = "",
    contact_presence: bool = True,
    team_color: str = "Cyan",
    team_role: str = "Team Member",
) -> bytes:
    """Return a CoT <event> document as UTF-8 bytes.

    With ``contact_presence`` (default) the event is shaped like a TAK
    client's own position report (PLI): it carries ``<takv>``, a
    server-routable ``<contact endpoint>``, ``<uid Droid>`` and
    ``<__group>``. That is what makes the callsign appear in ATAK/WinTAK's
    contacts list instead of being just an anonymous map marker.
    """
    now = datetime.now(timezone.utc)
    start = fix_time or now
    stale = now + timedelta(seconds=stale_seconds)

    ce = f"{accuracy_m:.1f}" if accuracy_m and accuracy_m > 0 else UNKNOWN

    if contact_presence:
        detail_parts = [
            '<takv device="Xplora watch" platform="xplora2tak" os="linux" version="1.2.0"/>',
            f'<contact callsign={quoteattr(callsign)} endpoint="*:-1:stcp"/>',
            f"<uid Droid={quoteattr(callsign)}/>",
            '<precisionlocation altsrc="GPS" geopointsrc="GPS"/>',
            f"<__group name={quoteattr(team_color)} role={quoteattr(team_role)}/>",
        ]
    else:
        detail_parts = [f"<contact callsign={quoteattr(callsign)}/>"]
    if battery is not None:
        detail_parts.append(f'<status battery="{int(battery)}"/>')
    if remarks:
        detail_parts.append(f"<remarks>{escape(remarks)}</remarks>")
    detail = "".join(detail_parts)

    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<event version=\"2.0\" uid={quoteattr(uid)} type={quoteattr(cot_type)} "
        f'how="m-g" time="{now.strftime(COT_TIME_FMT)[:-4]}Z" '
        f'start="{start.strftime(COT_TIME_FMT)[:-4]}Z" '
        f'stale="{stale.strftime(COT_TIME_FMT)[:-4]}Z">'
        f'<point lat="{lat:.7f}" lon="{lon:.7f}" hae="{UNKNOWN}" '
        f'ce="{ce}" le="{UNKNOWN}"/>'
        f"<detail>{detail}</detail>"
        "</event>"
    )
    return xml.encode("utf-8")


class TakSender:
    """Fire-and-forget CoT delivery. A fresh connection is made per batch,
    which is fine at multi-minute polling intervals and avoids stale-socket
    handling."""

    def __init__(self, options: dict[str, Any]) -> None:
        self.host: str = options.get("host") or ""
        self.port: int = int(options.get("port") or 8087)
        self.protocol: str = (options.get("protocol") or "tcp").lower()
        self.tls_ca_file: str = options.get("tls_ca_file") or ""
        self.tls_cert_file: str = options.get("tls_cert_file") or ""
        self.tls_key_file: str = options.get("tls_key_file") or ""
        self.tls_p12_file: str = options.get("tls_p12_file") or ""
        self.tls_p12_password: str = options.get("tls_p12_password") or ""
        self.tls_verify: bool = bool(options.get("tls_verify", True))
        if not self.host:
            raise ValueError("TAK output enabled but tak.host is not set")

        if self.protocol == "tls":
            missing = [
                f"{name}: {path}"
                for name, path in (
                    ("tls_ca_file", self.tls_ca_file),
                    ("tls_cert_file", self.tls_cert_file),
                    ("tls_key_file", self.tls_key_file),
                    ("tls_p12_file", self.tls_p12_file),
                )
                if path and not os.path.isfile(path)
            ]
            if missing:
                raise ValueError(
                    "TLS certificate file(s) not found inside the add-on: "
                    + "; ".join(missing)
                    + ". Put certificates in Home Assistant's ssl folder and "
                    "reference them as /ssl/<filename> (the add-on can only "
                    "see /ssl and /share)."
                )
            if self.tls_p12_file:
                self._load_p12()
        # The classic misconfiguration: TAK Server listens for plain TCP on
        # 8087 and TLS (client certs required) on 8089. Bytes sent with the
        # wrong protocol are silently discarded server-side, so warn loudly.
        if self.protocol == "tcp" and self.port == 8089:
            _LOGGER.warning(
                "tak.port 8089 is normally the TLS input but tak.protocol is "
                "'tcp' - the server will silently drop these events. Set "
                "protocol: tls (with certificates) or port: 8087."
            )
        elif self.protocol == "tls" and self.port == 8087:
            _LOGGER.warning(
                "tak.port 8087 is normally the plain-TCP input but "
                "tak.protocol is 'tls' - set protocol: tcp or port: 8089."
            )

    def _load_p12(self) -> None:
        """Extract client cert/key (and CA chain) from a PKCS#12 bundle, as
        handed out by TAK Server enrollment / data packages, into PEM files
        the ssl module can load."""
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            pkcs12,
        )

        with open(self.tls_p12_file, "rb") as fh:
            blob = fh.read()
        password = self.tls_p12_password.encode() or None
        try:
            key, cert, extra_certs = pkcs12.load_key_and_certificates(blob, password)
        except ValueError as exc:
            raise ValueError(
                f"Could not read {self.tls_p12_file}: {exc}. Check "
                "tls_p12_password (TAK Server bundles often use 'atakatak')."
            ) from exc
        if key is None or cert is None:
            raise ValueError(
                f"{self.tls_p12_file} does not contain a client key + "
                "certificate pair"
            )

        pem_dir = os.environ.get("XPLORA2TAK_DATA", "/data")
        cert_path = os.path.join(pem_dir, "tak_client.pem")
        with open(cert_path, "wb") as fh:
            fh.write(cert.public_bytes(Encoding.PEM))
            fh.write(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        os.chmod(cert_path, 0o600)
        self.tls_cert_file = cert_path
        self.tls_key_file = ""

        if extra_certs and not self.tls_ca_file:
            ca_path = os.path.join(pem_dir, "tak_ca.pem")
            with open(ca_path, "wb") as fh:
                for ca in extra_certs:
                    fh.write(ca.public_bytes(Encoding.PEM))
            self.tls_ca_file = ca_path
        _LOGGER.info(
            "Loaded client certificate from %s (CN=%s)",
            self.tls_p12_file,
            cert.subject.rfc4514_string(),
        )

    def send(self, events: list[bytes]) -> None:
        if not events:
            return
        payload = b"".join(events)
        if self.protocol == "udp":
            self._send_udp(events)
        elif self.protocol == "tls":
            self._send_stream(payload, use_tls=True)
        else:
            self._send_stream(payload, use_tls=False)
        _LOGGER.debug("Sent %d CoT event(s) to %s:%d", len(events), self.host, self.port)

    def _send_udp(self, events: list[bytes]) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for event in events:
                sock.sendto(event, (self.host, self.port))

    def _send_stream(self, payload: bytes, use_tls: bool) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=15)
        try:
            if use_tls:
                context = ssl.create_default_context(
                    cafile=self.tls_ca_file or None
                )
                if not self.tls_verify:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                if self.tls_cert_file:
                    context.load_cert_chain(
                        self.tls_cert_file, self.tls_key_file or None
                    )
                sock = context.wrap_socket(raw, server_hostname=self.host)
                _LOGGER.debug(
                    "TLS handshake with %s:%d ok (%s)",
                    self.host,
                    self.port,
                    sock.version(),
                )
            else:
                sock = raw
            sock.sendall(payload)
            if use_tls:
                # Send TLS close_notify so the server flushes and processes
                # everything before the connection drops.
                try:
                    sock.unwrap()
                except (OSError, ssl.SSLError):
                    pass
        finally:
            raw.close()
