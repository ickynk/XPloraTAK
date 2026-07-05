"""Minimal, well-behaved client for the (unofficial) Xplora GraphQL API.

Design goals, learned from the demise of the Ludy87/xplora_watch integration
(archived after Xplora started IP-banning clients of the reverse-engineered
API):

  * Passive reads only. We query ``watchLastLocate`` (the location the watch
    last reported on its own schedule) and NEVER send locate commands that
    ping the watch. Active locates are high-signal, battery-draining traffic.
  * Log in as rarely as possible. The old integration treated tokens as
    expired after 240 seconds and re-authenticated constantly. We cache the
    issued token on disk, reuse it until the API rejects it, and enforce a
    hard floor between login attempts.
  * Fail slow. Auth failures and HTTP 403s back off for a long time instead
    of hammering the endpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

_LOGGER = logging.getLogger(__name__)

ENDPOINT = "https://api.myxplora.com/api"
API_KEY = "fc45d50304511edbf67a12b93c413b6a"
API_SECRET = "1e9b6fe0327711ed959359c157878dcb"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

LOGIN_HINTS = (
    "Hints: use exactly the credentials that work in the Xplora phone app; "
    "accounts created with Google/Apple sign-in have no password and cannot "
    "log in here (set a password in the app first); country_code must have "
    "no '+'; if you registered with a phone number, try phone login instead "
    "of email (and vice versa)."
)

# Hard floor between login attempts. Re-authenticating in a tight loop is the
# most likely way to get an IP banned, so even if the API keeps rejecting us
# we refuse to sign in more often than this.
MIN_LOGIN_INTERVAL = 15 * 60

SIGN_IN_MUTATION = """
mutation signInWithEmailOrPhone($countryPhoneNumber: String, $phoneNumber: String, $password: String!, $emailAddress: String, $client: ClientType!, $userLang: String!, $timeZone: String!) {
  signInWithEmailOrPhone(countryPhoneNumber: $countryPhoneNumber, phoneNumber: $phoneNumber, password: $password, emailAddress: $emailAddress, client: $client, userLang: $userLang, timeZone: $timeZone) {
    id
    token
    issueDate
    expireDate
    user {
      id
      name
      children {
        ward {
          id
          name
          phoneNumber
        }
      }
    }
  }
}
"""

READ_MY_INFO_QUERY = """
query ReadMyInfo {
  readMyInfo {
    id
    name
    children {
      ward {
        id
        name
        phoneNumber
      }
    }
  }
}
"""

WATCH_LAST_LOCATE_QUERY = """
query WatchLastLocate($uid: String!) {
  watchLastLocate(uid: $uid) {
    tm
    lat
    lng
    rad
    country
    province
    city
    addr
    poi
    battery
    isCharging
    isAdjusted
    locateType
    step
    distance
    isInSafeZone
    safeZoneLabel
  }
}
"""


class XploraError(Exception):
    """Generic API failure."""


class XploraAuthError(XploraError):
    """Credentials rejected or token invalid and re-login not yet allowed."""


class XploraBlockedError(XploraError):
    """The API returned 403 - possibly an IP block. Back off hard."""


class XploraClient:
    def __init__(
        self,
        password: str,
        email: str = "",
        country_code: str = "",
        phone_number: str = "",
        user_lang: str = "en-US",
        timezone_name: str = "UTC",
        cache_path: str = "/data/xplora_token.json",
    ) -> None:
        if not password or not (email or (country_code and phone_number)):
            raise XploraAuthError(
                "Provide a password plus either an email address or "
                "country_code + phone_number in the add-on configuration."
            )
        # Normalize: stray whitespace and a '+' on the country code are the
        # most common config mistakes and the API rejects them.
        self._email = (email or "").strip()
        self._country_code = (country_code or "").strip().lstrip("+")
        self._phone_number = (phone_number or "").strip().replace(" ", "")
        self._password_md5 = hashlib.md5(password.encode()).hexdigest()
        self._user_lang = user_lang
        self._timezone = timezone_name
        self._cache_path = cache_path

        self._token: Optional[str] = None
        self._expire_at: float = 0.0
        self._user: dict[str, Any] = {}
        self._last_login_attempt: float = 0.0

        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

        self._load_cache()

    # ------------------------------------------------------------------ cache

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if data.get("account_hash") != self._account_hash():
            _LOGGER.info("Cached token belongs to different credentials; ignoring")
            return
        self._token = data.get("token")
        self._expire_at = float(data.get("expire_at", 0))
        self._user = data.get("user", {})
        self._last_login_attempt = float(data.get("last_login_attempt", 0))
        if self._token:
            _LOGGER.info(
                "Reusing cached Xplora token (expires %s)",
                datetime.fromtimestamp(self._expire_at, timezone.utc).isoformat()
                if self._expire_at
                else "unknown",
            )

    def _save_cache(self) -> None:
        data = {
            "account_hash": self._account_hash(),
            "token": self._token,
            "expire_at": self._expire_at,
            "user": self._user,
            "last_login_attempt": self._last_login_attempt,
        }
        tmp = self._cache_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self._cache_path)
        except OSError as exc:
            _LOGGER.warning("Could not persist token cache: %s", exc)

    def _account_hash(self) -> str:
        ident = f"{self._email}|{self._country_code}|{self._phone_number}|{self._password_md5}"
        return hashlib.sha256(ident.encode()).hexdigest()

    # ------------------------------------------------------------- transport

    def _request_headers(self) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        if self._token:
            auth = f"Bearer {self._token}:{API_SECRET}"
        else:
            auth = f"Open {API_KEY}:{API_SECRET}"
        return {
            "H-Date": now.strftime("%a, %d %b %Y %H:%M:%S GMT"),
            "H-Tid": str(math.floor(time.time())),
            "H-BackDoor-Authorization": auth,
            "Content-Type": "application/json; charset=UTF-8",
        }

    def _gql(
        self, query: str, variables: dict[str, Any], operation_name: str
    ) -> dict[str, Any]:
        payload = {
            "query": query,
            "variables": variables,
            "operationName": operation_name,
        }
        try:
            resp = self._session.post(
                ENDPOINT,
                json=payload,
                headers=self._request_headers(),
                timeout=60,
            )
        except requests.RequestException as exc:
            raise XploraError(f"Network error talking to Xplora API: {exc}") from exc

        if resp.status_code == 403:
            raise XploraBlockedError(
                "Xplora API returned HTTP 403. This may indicate an IP block. "
                "Backing off - do NOT lower the poll interval."
            )
        if resp.status_code in (401,):
            raise XploraAuthError("Xplora API returned HTTP 401 (unauthorized)")
        if resp.status_code != 200:
            raise XploraError(f"Xplora API returned HTTP {resp.status_code}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise XploraError("Xplora API returned a non-JSON response") from exc

        errors = body.get("errors") or []
        if errors:
            messages = "; ".join(str(e.get("message", e)) for e in errors)
            lowered = messages.lower()
            if "auth" in lowered or "token" in lowered or "permission" in lowered:
                raise XploraAuthError(f"Xplora API auth error: {messages}")
            raise XploraError(f"Xplora API error: {messages}")

        return body.get("data") or {}

    # ------------------------------------------------------------------ auth

    def login(self) -> None:
        wait = MIN_LOGIN_INTERVAL - (time.time() - self._last_login_attempt)
        if wait > 0:
            raise XploraAuthError(
                f"Refusing to sign in again for another {int(wait)}s "
                "(login rate limit, protects against IP bans)"
            )
        self._last_login_attempt = time.time()
        self._token = None
        self._save_cache()

        # Match pyxplora_api's proven payload semantics exactly: phone fields
        # are empty strings when unused, but emailAddress must be JSON null
        # (not "") or the server can take the email-auth path and reject the
        # login with "Authentication failed."
        variables = {
            "countryPhoneNumber": self._country_code,
            "phoneNumber": self._phone_number,
            "password": self._password_md5,
            "emailAddress": self._email or None,
            "userLang": self._user_lang,
            "timeZone": self._timezone,
            "client": "APP",
        }
        _LOGGER.info(
            "Signing in to Xplora API as %s",
            self._email if self._email else f"+{self._country_code} {self._phone_number}",
        )
        try:
            data = self._gql(SIGN_IN_MUTATION, variables, "signInWithEmailOrPhone")
        except XploraAuthError as exc:
            raise XploraAuthError(f"{exc} {LOGIN_HINTS}") from exc
        issue = data.get("signInWithEmailOrPhone")
        if not issue or not issue.get("token"):
            raise XploraAuthError(f"Sign-in failed. {LOGIN_HINTS}")
        self._token = issue["token"]
        self._expire_at = _parse_epoch(issue.get("expireDate"))
        self._user = issue.get("user") or {}
        self._save_cache()
        _LOGGER.info("Signed in successfully")

    def _authed(self, query: str, variables: dict[str, Any], op: str) -> dict[str, Any]:
        """Run a query, signing in (at most once) if the token is missing/stale."""
        if not self._token:
            self.login()
            return self._gql(query, variables, op)
        try:
            return self._gql(query, variables, op)
        except XploraAuthError:
            _LOGGER.info("Token rejected, attempting one re-login")
            self.login()
            return self._gql(query, variables, op)

    # ------------------------------------------------------------------- api

    def get_watches(self) -> list[dict[str, Any]]:
        """Return [{'id': wuid, 'name': ..., 'phoneNumber': ...}, ...]."""
        children = (self._user or {}).get("children")
        if not children and not self._token:
            # The sign-in response already carries the children list; logging
            # in here avoids a separate ReadMyInfo round trip.
            self.login()
            children = (self._user or {}).get("children")
        if not children:
            data = self._authed(READ_MY_INFO_QUERY, {}, "ReadMyInfo")
            me = data.get("readMyInfo") or {}
            children = me.get("children") or []
            if children:
                self._user = me
                self._save_cache()
        watches = []
        for child in children or []:
            ward = (child or {}).get("ward") or {}
            if ward.get("id"):
                watches.append(
                    {
                        "id": ward["id"],
                        "name": ward.get("name") or ward["id"],
                        "phoneNumber": ward.get("phoneNumber") or "",
                    }
                )
        return watches

    def get_last_location(self, wuid: str) -> Optional[dict[str, Any]]:
        """Passive read of the last location the watch reported. Never pings
        the watch itself."""
        data = self._authed(WATCH_LAST_LOCATE_QUERY, {"uid": wuid}, "WatchLastLocate")
        return data.get("watchLastLocate")


def _parse_epoch(value: Any) -> float:
    """issueDate/expireDate come back as epoch seconds or milliseconds."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if num > 1e12:  # milliseconds
        num /= 1000.0
    return num
