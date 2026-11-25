"""Cap every request body before the app reads it.

FastAPI reads and parses a route's body before its auth dependency runs, and
Starlette spools file parts to disk, so without a cap any client could make a
worker hold or store a body of any size before a credential is checked. Every
request gets ``max_request_body_mb``. The upload routes get their larger cap
only for a request carrying a live session or API token; without one, a body
over the default is answered 401, so the console refreshes and retries.
"""

import json
import re
from typing import List, Tuple

from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.config import get_settings

_MB = 1024 * 1024
# Multipart framing and the form fields beside the file.
_SLACK = _MB
# The signed webhooks are machine callers with no session. Their receiver
# stops reading at its own configured cap; this is the highest the settings allow.
_WEBHOOK_CAP = 16384 * 1024


def _root() -> str:
    return re.escape(get_settings().vigil_context_path.rstrip("/"))


def _upload_caps() -> List[Tuple[re.Pattern, int]]:
    from core.auth.breached_list import MAX_BYTES as BREACHED_LIST_MAX
    from core.documents.extract import MAX_DOCUMENT_BYTES

    s = get_settings()
    caps = [
        (r"/api/ingest/upload", s.max_upload_size_mb * _MB),
        (r"/api/session-policy/breached-list", BREACHED_LIST_MAX),
        (r"/api/cases/[^/]+/attachments", MAX_DOCUMENT_BYTES),
        (r"/api/workflows/threat-hunt/document", MAX_DOCUMENT_BYTES),
    ]
    return [(re.compile(_root() + p + "/?"), n + _SLACK) for p, n in caps]


def _has_credential(conn: HTTPConnection) -> bool:
    """A live session or API token, checked without reading the body. The
    route's own dependency still decides what the caller may do."""
    from core.auth.auth_cookies import ACCESS_COOKIE_NAME
    from core.auth.auth_service import AuthService
    from core.auth.current_user import ACCESS_TOKEN_TYPE, DEV_MODE, _check_sign_in
    from core.auth.mcp_credential_service import authenticate, looks_like_api_token

    if DEV_MODE:
        return True
    auth = conn.headers.get("authorization", "")
    bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else None
    if bearer and looks_like_api_token(bearer):
        return authenticate(bearer) is not None
    token = conn.cookies.get(ACCESS_COOKIE_NAME) or bearer
    payload = AuthService.verify_jwt_token(token) if token else None
    if not payload or payload.get("token_type") != ACCESS_TOKEN_TYPE:
        return False
    if not payload.get("sid") or not payload.get("user_id"):
        return False
    # The sign-in must still be open; not counted as activity, and the MFA
    # requirement is left to the route.
    _, refusal = _check_sign_in(
        payload["user_id"], payload["sid"], conn.headers.get("user-agent"), False, True
    )
    return refusal is None


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.default = max(1, get_settings().max_request_body_mb) * _MB
        self.uploads = _upload_caps()
        self.webhooks = re.compile(_root() + r"/api/webhooks/.*")

    async def _limit(self, scope: Scope) -> Tuple[int, int]:
        """The cap, and the status to refuse past it with."""
        path = scope.get("path", "")
        if self.webhooks.fullmatch(path):
            return max(self.default, _WEBHOOK_CAP), 413
        cap = next((n for p, n in self.uploads if p.fullmatch(path)), None)
        if cap is None or cap <= self.default:
            return self.default, 413
        ok = await run_in_threadpool(_has_credential, HTTPConnection(scope))
        return (cap, 413) if ok else (self.default, 401)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        limit, refusal = await self._limit(scope)
        declared = HTTPConnection(scope).headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await self._refuse(send, limit, refusal)
            return

        received = 0
        over = started = False

        async def capped_receive() -> Message:
            # Past the cap the app is told the client left, so it stops reading
            # wherever it is; its answer is dropped and the 413 sent instead.
            nonlocal received, over
            if over:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    over = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if over and not started:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, capped_receive, guarded_send)
        except Exception:
            if not over:
                raise
        if over and not started:
            await self._refuse(send, limit, refusal)

    @staticmethod
    async def _refuse(send: Send, limit: int, status: int) -> None:
        detail = (
            "Not authenticated"
            if status == 401
            else f"Request body exceeds {limit // _MB} MB"
        )
        body = json.dumps({"detail": detail})
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"connection", b"close"),
        ]
        if status == 401:
            headers.append((b"www-authenticate", b"Bearer"))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body.encode()})
