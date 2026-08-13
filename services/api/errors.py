"""Application-wide exception handlers.

Route handlers raise ``core.exceptions.SOCError`` subclasses for expected
failures and let anything else propagate. These handlers render both as JSON,
so no route needs its own ``try``/``except`` to produce an HTTP error response.

Unexpected exceptions never put their text in the response body: the detail is
logged with the OTEL trace id, and the client gets that id to quote instead.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.exceptions import SOCError
from core.telemetry import current_trace_ids

logger = logging.getLogger(__name__)


async def soc_error_handler(request: Request, exc: SOCError) -> JSONResponse:
    status = getattr(exc, "status_code", 500)
    body: dict[str, str] = {"detail": exc.message, "code": exc.code}

    trace_id, _ = current_trace_ids()
    if trace_id:
        body["trace_id"] = trace_id

    log = logger.exception if status >= 500 else logger.warning
    log("%s %s -> %d: %s", request.method, request.url.path, status, exc.message)
    return JSONResponse(status_code=status, content=body)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id, _ = current_trace_ids()
    logger.exception(
        "Unhandled error: %s %s", request.method, request.url.path, exc_info=exc
    )
    body = {"detail": "Internal server error", "code": "INTERNAL_ERROR"}
    if trace_id:
        body["trace_id"] = trace_id
    return JSONResponse(status_code=500, content=body)


class CatchUnhandledMiddleware:
    """Render unhandled exceptions as JSON from inside the CORS layer.

    Starlette runs the ``Exception`` handler in ServerErrorMiddleware, which is
    the outermost layer — its response never passes back through CORSMiddleware,
    so a browser sees an opaque CORS failure instead of the 500. This catches
    the exception below CORS, leaving the response to be decorated on the way
    out. Must therefore be added *before* CORSMiddleware.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def _send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            if started:  # headers already flushed; nothing sendable remains
                raise
            request = Request(scope, receive)
            response = await unhandled_error_handler(request, exc)
            await response(scope, receive, send)


def register_exception_handlers(app: FastAPI) -> None:
    """Register the domain-error handler. Call before adding CORSMiddleware."""
    app.add_exception_handler(SOCError, soc_error_handler)  # type: ignore[arg-type]
    # Terminal net for the case above where the response had already started.
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_middleware(CatchUnhandledMiddleware)
