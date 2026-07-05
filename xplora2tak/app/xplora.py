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

# Endpoint and key pair as used by clients verified working against the
# 2026 API (the old api.myxplora.com host + 2022 key pair now yields
# "Authentication failed."). Overridable via the add-on's api options.
ENDPOINT = "https://api.prod.myxplora.com/api"
API_KEY = "63fa1d10289711ea80b5992f808043b2"
API_SECRET = "27ed7670379511eab4a0f367f8eb1312"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

LOGIN_HINTS = (
    "Hints: the current Xplora API only accepts email + password logins - "
    "use the email registered on the account; accounts created with "
    "Google/Apple sign-in have no password and cannot log in here (set a "
    "password in the app first); use exactly the password that works in the "
    "Xplora phone app."
)

# Hard floor between login attempts. Re-authenticating in a tight loop is the
# most likely way to get an IP banned, so even if the API keeps rejecting us
# we refuse to sign in more often than this.
MIN_LOGIN_INTERVAL = 15 * 60

# Email sign-in: the current API expects the WEB client type and no phone
# variables at all on this path.
EMAIL_SIGN_IN_MUTATION = """
mutation signInWithEmailOrPhone($emailAddress: String, $password: String!, $client: ClientType!, $userLang: String!, $timeZone: String!) {
  signInWithEmailOrPhone(emailAddress: $emailAddress, password: $password, client: $client, userLang: $userLang, timeZone: $timeZone) {
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
    w360 {
      token
      secret
      qid
    }
  }
}
"""

# Legacy phone sign-in. Reportedly no longer accepted by the current API,
# kept as a fallback for accounts that cannot use email.
PHONE_SIGN_IN_MUTATION = """
mutation signInWithEmailOrPhone($countryPhoneNumber: String, $phoneNumber: String, $password: String!, $client: ClientType!, $userLang: String!, $timeZone: String!) {
  signInWithEmailOrPhone(countryPhoneNumber: $countryPhoneNumber, phoneNumber: $phoneNumber, password: $password, client: $client, userLang: $userLang, timeZone: $timeZone) {
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
    w360 {
      token
      secret
      qid
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


class XploraLoginRateLimited(XploraAuthError):
    """A sign-in was refused by the local rate limiter; retry after `wait`."""

    def __init__(self, wait: float) -> None:
        self.wait = max(1.0, wait)
        super().__init__(
            f"Sign-in postponed for {int(self.wait)}s "
            "(login rate limit, protects against IP bans)"
        )


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
        endpoint: str = "",
        api_key: str = "",
        api_secret: str = "",
    ) -> None:
        if not password or not (email or (country_code and phone_number)):
            raise XploraAuthError(
                "Provide a password plus either an email address or "
                "country_code + phone_number in the add-on configuration."
            )
        # Normalize: stray whitespace, a '+' on the country code and
        # mixed-case email are the most common config mistakes; the API
        # matches the email exactly, so lowercase it like the app does.
        self._email = (email or "").strip().lower()
        self._country_code = (country_code or "").strip().lstrip("+")
        self._phone_number = (phone_number or "").strip().replace(" ", "")
        self._password_md5 = hashlib.md5(password.encode()).hexdigest()
        self._user_lang = user_lang
        self._timezone = timezone_name
        self._cache_path = cache_path
        self._endpoint = (endpoint or "").strip() or ENDPOINT
        self._api_key = (api_key or "").strip() or API_KEY
        self._api_secret = (api_secret or "").strip() or API_SECRET

        self._token: Optional[str] = None
        self._bearer_secret: str = self._api_secret
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
        self._bearer_secret = data.get("bearer_secret") or self._api_secret
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
            "bearer_secret": self._bearer_secret,
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
        ident = (
            f"{self._email}|{self._country_code}|{self._phone_number}|"
            f"{self._password_md5}|{self._endpoint}|{self._api_key}"
        )
        return hashlib.sha256(ident.encode()).hexdigest()

    # ------------------------------------------------------------- transport

    def _request_headers(self) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        if self._token:
            auth = f"Bearer {self._token}:{self._bearer_secret}"
        else:
            auth = f"Open {self._api_key}:{self._api_secret}"
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
                self._endpoint,
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
            _LOGGER.debug("GraphQL error response: %s", json.dumps(errors))
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
            raise XploraLoginRateLimited(wait)
        self._last_login_attempt = time.time()
        self._token = None
        self._save_cache()

        # The current (2026) API distinguishes the two sign-in paths: email
        # logins use the WEB client type with no phone variables at all;
        # the legacy phone path is reportedly no longer accepted but is kept
        # as a fallback.
        if self._email:
            mutation = EMAIL_SIGN_IN_MUTATION
            variables = {
                "emailAddress": self._email,
                "password": self._password_md5,
                "userLang": self._user_lang,
                "timeZone": self._timezone,
                "client": "WEB",
            }
            identity = self._email
        else:
            mutation = PHONE_SIGN_IN_MUTATION
            variables = {
                "countryPhoneNumber": self._country_code,
                "phoneNumber": self._phone_number,
                "password": self._password_md5,
                "userLang": self._user_lang,
                "timeZone": self._timezone,
                "client": "APP",
            }
            identity = f"+{self._country_code} {self._phone_number}"
            _LOGGER.warning(
                "Signing in by phone number; the current Xplora API may only "
                "accept email logins - configure auth.email if this fails"
            )
        _LOGGER.info("Signing in to Xplora API as %s", identity)
        try:
            data = self._gql(mutation, variables, "signInWithEmailOrPhone")
        except XploraAuthError as exc:
            raise XploraAuthError(f"{exc} {LOGIN_HINTS}") from exc
        issue = data.get("signInWithEmailOrPhone")
        if not issue or not issue.get("token"):
            raise XploraAuthError(f"Sign-in failed. {LOGIN_HINTS}")
        self._token = issue["token"]
        # Subsequent requests must be signed with the w360 secret when the
        # account has one; the static API secret is only the fallback.
        w360 = issue.get("w360") or {}
        self._bearer_secret = w360.get("secret") or self._api_secret
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
