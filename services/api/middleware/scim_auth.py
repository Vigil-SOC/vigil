"""Count a failed authentication on the directory-sync endpoint (#4423).

The handler never runs when the bearer is refused, so the note is taken here.
The body and the Authorization header are not read and not logged.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from core.auth import trail_events


class ScimAuthNoteMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if response.status_code == 401 and "/scim/" in path:
            trail_events.refused("directory.auth_failed", reason="unauthenticated")
        return response
