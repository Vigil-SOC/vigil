"""The gateway's own credential for the backend (SP1 §1, decisions 2 and 3; #4425).

Each request picks the credential from the files right then: a readable,
non-empty token file means that token, sent as is (no login, no refresh);
otherwise a readable, non-empty password file (with a user) means the Viewer
login and refresh; otherwise `no_credential`. A refused token never falls
through to the password.

Medic never holds this credential. A failure is reported through the state
(`GET /_gw/status`), never hammered. The gateway uses a service account, which
is not locked, and the password path still stops after BAD_PASSWORD_STOP 401s.
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
TOKEN_RETRY = 300  # a refused token is tried again after this, or once its file changes


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
    def __init__(
        self,
        upstream,
        user: str | None,
        password_file: Path,
        token_file: Path,
        clock=time.time,
    ):
        self.upstream, self.user, self.clock = upstream, user, clock
        self.pw_file, self.tok_file = password_file, token_file
        self.lock = threading.Lock()
        self.access: str | None = None
        self.refresh_tok: str | None = None
        self.access_exp = self.margin = self.minted_at = self.next_login_at = 0.0
        self.state, self.since, self.bad_pw, self.pw_mtime = "init", clock(), 0, None
        self.credential = "none"
        self.file_tok: str | None = None  # last token read from the file (remembered)
        self.tok_mtime: float | None = None
        self.refused: tuple[str, float | None] | None = None  # (token, mtime) on a 401
        self.tok_retry_at = 0.0
        # The password path's (state, next_login_at), kept while another credential
        # is in use, so a switch back doesn't lift its stop or backoff.
        self.pw_saved: tuple[str, float] | None = None

    def _set(self, state: str, retry_in: float = 0.0) -> None:
        self.next_login_at = self.clock() + retry_in
        if state != self.state:
            level = logging.INFO if state in ("ok", "init") else logging.WARNING
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

    def _file_token(self) -> str | None:
        """The token file's content, or None if it is missing, empty or unreadable."""
        try:
            mtime = os.stat(self.tok_file).st_mtime
            tok = Path(self.tok_file).read_text().strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not tok:
            return None
        if tok != self.file_tok:
            logs.forget(self.file_tok)
            logs.remember(tok)
            self.file_tok = tok
        self.tok_mtime = mtime
        return tok

    def _password(self) -> str | None:
        if not self.user:  # the user is needed only here: without it, no password
            return None
        try:
            mtime = os.stat(self.pw_file).st_mtime
            password = Path(self.pw_file).read_text().strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not password:  # an empty file is absent, never sent
            return None
        if self.bad_pw >= BAD_PASSWORD_STOP and mtime != self.pw_mtime:
            self.next_login_at, self.bad_pw = 0.0, BAD_PASSWORD_STOP - 1  # one attempt
            self.pw_saved = None
        self.pw_mtime = mtime
        logs.remember(password)
        return password

    def _login(self, password: str) -> None:
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

    def _select(self) -> tuple[str | None, str | None]:
        """(token, password) from the files now; at most one is set."""
        tok = self._file_token()
        password = None if tok else self._password()
        cred = "token" if tok else "password" if password else "none"
        if cred != self.credential:  # a new credential starts afresh
            logs.event("auth_credential", credential=cred, previous=self.credential)
            if self.credential == "password":
                self.pw_saved = (self.state, self.next_login_at)
            self.credential = cred
            saved = self.pw_saved if cred == "password" else None
            if saved:
                self._set(saved[0])
                self.next_login_at = saved[1]
            else:
                self._set("init")
            if cred == "password":
                self.pw_saved = None
        return tok, password

    def _blocked(self, tok: str) -> bool:
        """`tok` was refused, its file is unchanged, and the wait isn't over."""
        return (
            self.refused == (tok, self.tok_mtime) and self.clock() < self.tok_retry_at
        )

    def token(self) -> str:
        with self.lock:
            tok, password = self._select()
            now = self.clock()
            if tok:
                if self._blocked(tok):
                    raise AuthUnavailable(self.state)
                return tok
            if password is None:
                self._set("no_credential")
                raise AuthUnavailable(self.state)
            if self.access and now < self.access_exp - self.margin:
                return self.access
            if self.state == "rejected_after_login" and now < self.next_login_at:
                raise AuthUnavailable(self.state)
            if not (self.refresh_tok and self._refresh()):
                self._login(password)
            return self.access

    def _refuse(self, token: str) -> None:
        # Even if the file has just emptied (mid-rewrite), this request stops here.
        self.refused = (token, self.tok_mtime)
        self.tok_retry_at = self.clock() + TOKEN_RETRY
        if self.credential == "token":
            self._set("unauthorized")

    def accepted(self, token: str) -> None:
        """The backend took `token` (not a 401). Only the token path learns from it;
        a late reply sent before a refusal doesn't lift it."""
        with self.lock:
            ours = self.credential == "token" and token == self.file_tok
            if ours and not self._blocked(token):
                self.refused = None
                self._set("ok")

    def rejected(self, token: str) -> bool:
        """The backend said 401 to `token`; True means "get a new one and retry once".

        A refused file token is never retried at once, and never swapped for the
        password. A password-path token minted seconds ago that is refused points
        at the backend's revocation store (Redis down fails closed), not at us:
        back off instead of logging in."""
        with self.lock:
            if token == self.file_tok:
                self._refuse(token)
                return False
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
            if token == self.file_tok:
                return self._refuse(token)
            if token == self.access:
                self.access, self.access_exp = None, 0.0
            self._set("rejected_after_login", 60)

    def status(self) -> dict:
        # The credential the files pick now, unless a login holds the lock: the
        # status read never waits on the backend.
        if self.lock.acquire(blocking=False):
            try:
                self._select()
            finally:
                self.lock.release()
        return {
            "state": self.state,
            "since": int(self.since),
            "credential": self.credential,
        }
