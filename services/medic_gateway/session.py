"""The gateway's own Viewer login and refresh (SP1 §1, decisions 2 and 3).

Medic never holds this credential. A failed login is reported through the state
(`GET /_gw/status`), never hammered: Vigil locks an account after 5 bad passwords
for 15 minutes, its counter only resets on success (D2-17), and it allows 5
logins a minute per IP.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from services.medic_gateway import logs

BAD_PASSWORD_STOP = 2  # stop for good after this many 401s, until the secret changes
BACKOFF = {"bad_password": 300, "backend_unreachable": 30, "login_error": 60}


class AuthUnavailable(Exception):
    pass


def refresh_margin(ttl: float) -> float:
    """Refresh this long before expiry: 120 s, or a quarter of a short token's life."""
    return min(120, ttl / 4)


def _parse(body: bytes, now: float) -> tuple[str, str, float]:
    """(access, refresh, life in seconds) from a login or refresh reply, or raise.

    The life is the token's own exp - iat, so a backend clock that differs from
    ours doesn't make every token look expired (review #8)."""
    reply = json.loads(body)
    access, refresh = reply["access_token"], reply["refresh_token"]
    if not (isinstance(access, str) and isinstance(refresh, str)):
        raise TypeError("token is not a string")
    part = access.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    life = float(claims["exp"]) - float(claims.get("iat", now))
    return access, refresh, life if life > 0 else 60.0


class ViewerSession:
    def __init__(self, upstream, user: str, password_file: Path, clock=time.time):
        self.upstream, self.user, self.pw_file, self.clock = (
            upstream,
            user,
            password_file,
            clock,
        )
        self.lock = threading.Lock()
        self.access: str | None = None
        self.refresh_tok: str | None = None
        self.access_exp = self.margin = self.minted_at = self.next_login_at = 0.0
        self.state, self.since, self.bad_pw, self.pw_mtime = "init", clock(), 0, None

    def _set(self, state: str, retry_in: float = 0.0) -> None:
        self.next_login_at = self.clock() + retry_in
        if state != self.state:
            level = logging.INFO if state == "ok" else logging.WARNING
            logs.event("auth_state", level, state=state, previous=self.state)
            self.state, self.since = state, self.clock()

    def _store(self, access: str, refresh: str, life: float) -> None:
        for t in (self.access, self.refresh_tok):
            logs.forget(t)
        self.access, self.refresh_tok = access, refresh
        logs.remember(access, refresh)
        self.minted_at = self.clock()
        self.access_exp = self.minted_at + life  # on our clock
        self.margin = refresh_margin(life)
        self.bad_pw = 0
        self._set("ok")

    def _post(self, path: str, payload: dict):
        csrf = secrets.token_urlsafe(32)  # double-submit pair: Vigil compares the two
        headers = {
            "Content-Type": "application/json",
            "Cookie": f"csrf_token={csrf}",
            "X-CSRF-Token": csrf,
        }
        return self.upstream.request(
            "POST", path, headers, json.dumps(payload).encode()
        )

    def _password(self) -> str | None:
        try:
            mtime = os.stat(self.pw_file).st_mtime
            password = Path(self.pw_file).read_text().strip()
        except OSError:
            return None
        if self.state == "bad_password_stopped" and mtime != self.pw_mtime:
            self.next_login_at, self.bad_pw = 0.0, BAD_PASSWORD_STOP - 1  # one attempt
        self.pw_mtime = mtime
        logs.remember(password)
        return password

    def _login(self) -> None:
        password = self._password()
        if password is None:
            if self.state != "bad_password_stopped":
                self._set("no_password", 60)
            raise AuthUnavailable(self.state)
        if self.clock() < self.next_login_at:
            raise AuthUnavailable(self.state)
        try:
            payload = {"username_or_email": self.user, "password": password}
            status, headers, body = self._post("/api/auth/login", payload)
            if status == 200:
                return self._store(*_parse(body, self.clock()))
        except OSError:
            self._set("backend_unreachable", BACKOFF["backend_unreachable"])
            raise AuthUnavailable(self.state) from None
        except Exception:  # noqa: BLE001 - any reply we can't use: back off (review #2)
            self._set("login_error", BACKOFF["login_error"])
            raise AuthUnavailable(self.state) from None
        retry_after = headers.get("retry-after", "")
        wait = int(retry_after) if retry_after.isdigit() else 60
        if status == 401:
            self.bad_pw += 1
            if self.bad_pw >= BAD_PASSWORD_STOP:  # far below Vigil's lockout of 5
                self._set("bad_password_stopped", float("inf"))
            else:
                self._set("bad_password", BACKOFF["bad_password"])
        elif status in (423, 429):
            self._set("locked" if status == 423 else "rate_limited", max(wait, 1))
        else:
            self._set("login_error", BACKOFF["login_error"])
        raise AuthUnavailable(self.state)

    def _refresh(self) -> bool:
        token, self.refresh_tok = self.refresh_tok, None  # single use either way
        try:
            status, _, body = self._post("/api/auth/refresh", {"refresh_token": token})
            if status == 200:
                self._store(*_parse(body, self.clock()))
                return True
        except Exception:  # noqa: BLE001 - a failed refresh falls back to one login
            return False
        return False

    def token(self) -> str:
        with self.lock:
            now = self.clock()
            if self.access and now < self.access_exp - self.margin:
                return self.access
            if self.state == "rejected_after_login" and now < self.next_login_at:
                raise AuthUnavailable(self.state)
            if not (self.refresh_tok and self._refresh()):
                self._login()
            return self.access

    def rejected(self, token: str) -> bool:
        """The backend said 401 to `token`; True means "get a new one and retry once".

        A token minted seconds ago that is refused points at the backend's revocation
        store (Redis down fails closed), not at us: back off instead of logging in."""
        with self.lock:
            if token != self.access:
                return True
            self.access, self.access_exp = None, 0.0
            if self.clock() - self.minted_at < 5:
                self._set("rejected_after_login", 60)
                return False
            return True

    def gave_up(self, token: str) -> None:
        """A fresh token, fetched after a 401, was refused too: back off (review #1)."""
        with self.lock:
            if token == self.access:
                self.access, self.access_exp = None, 0.0
            self._set("rejected_after_login", 60)

    def status(self) -> dict:
        return {"state": self.state, "since": int(self.since)}
