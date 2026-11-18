"""The OAuth authorization server's endpoints, at the host root (#4787, #4788).

The issuer is the bare origin (``oauth_urls``), so discovery, ``/authorize``,
``/token``, ``/register`` and ``/revoke`` sit at the root even when the console
is served under ``VIGIL_CONTEXT_PATH``. The protected-resource document sits at
``/.well-known/oauth-protected-resource{context}/mcp``.

A middleware ahead of routing, like ``mcp_surface.BareMountPath``, rather than
routes: the context path prefixes every router, and the SPA fallback turns a
GET 404 into the app shell. ``mcp.oauth_enabled`` flips at runtime, so the SDK
routes are built once and every request asks ``is_oauth_enabled()``; off, every
OAuth path is a 404.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Optional

from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from mcp.server.auth.errors import stringify_pydantic_error
from mcp.server.auth.handlers.metadata import (
    MetadataHandler,
    ProtectedResourceMetadataHandler,
)
from mcp.server.auth.handlers.revoke import RevocationRequest
from mcp.server.auth.middleware.client_auth import (
    AuthenticationError,
    ClientAuthenticator,
)
from mcp.server.auth.routes import (
    REVOCATION_PATH,
    _body_limited,
    _cors,
    build_metadata,
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from pydantic import AnyHttpUrl, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Router, request_response
from starlette.types import ASGIApp, Receive, Scope, Send

from core.auth.oauth_server import SCOPE, VigilOAuthProvider, request_address
from core.integrations.mcp.surface import is_oauth_enabled, oauth_urls
from services.api.middleware.client_address import client_host
from services.api.middleware.rate_limit import limiter

logger = logging.getLogger(__name__)

AS_METADATA = "/.well-known/oauth-authorization-server"
PRM_PREFIX = "/.well-known/oauth-protected-resource"
ENDPOINTS = ("/authorize", "/token", "/register", "/revoke")
# Also listed, unprefixed, as backend-served for the SPA fallback (main.py).
ROOT_PREFIXES = (AS_METADATA, PRM_PREFIX) + ENDPOINTS

# Per client address, on the shared limiter.
_LIMITS = {
    "/register": parse("20/hour"),
    "/token": parse("60/minute"),
    "/revoke": parse("30/minute"),
    "/authorize": parse("60/minute"),
}

_REGISTRATION = ClientRegistrationOptions(
    enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
)
_REVOCATION = RevocationOptions(enabled=True)


def claims(path: str) -> bool:
    """Whether ``path`` is one of the authorization server's addresses."""
    return (
        path in ENDPOINTS
        or path in (AS_METADATA, PRM_PREFIX)
        or path.startswith((AS_METADATA + "/", PRM_PREFIX + "/"))
    )


def _metadata_route(path: str, handler) -> Route:
    return Route(
        path,
        endpoint=cors_middleware(handler.handle, ["GET", "OPTIONS"]),
        methods=["GET", "OPTIONS"],
    )


class _PublicRevocation(RevocationRequest):
    # mcp 2.3.0 requires the field, so a public client's RFC 7009 request is a 400.
    client_secret: Optional[str] = None


def _revoke_route(provider: VigilOAuthProvider) -> Route:
    """The SDK's ``/revoke`` handler, with ``client_secret`` optional."""
    authenticator = ClientAuthenticator(provider)

    async def handle(request: Request) -> Response:
        try:
            client = await authenticator.authenticate_request(request)
        except AuthenticationError as e:
            return _json(
                401, {"error": "unauthorized_client", "error_description": e.message}
            )
        try:
            body = _PublicRevocation.model_validate(dict(await request.form()))
        except ValidationError as e:
            return _json(
                400,
                {
                    "error": "invalid_request",
                    "error_description": stringify_pydantic_error(e),
                },
            )
        loaders = [
            provider.load_access_token,
            partial(provider.load_refresh_token, client),
        ]
        if body.token_type_hint == "refresh_token":
            loaders.reverse()
        token = None
        for load in loaders:
            if (token := await load(body.token)) is not None:
                break
        # An unknown token, or another client's, is a 200 that revokes nothing (RFC 7009).
        if token is not None and token.client_id == client.client_id:
            await provider.revoke_token(token)
        return Response(
            status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )

    return Route(
        REVOCATION_PATH,
        endpoint=_cors(_body_limited(request_response(handle)), ["POST", "OPTIONS"]),
        methods=["POST", "OPTIONS"],
    )


def build_routes() -> list[Route]:
    """The SDK's routes, with both documents restated from ``oauth_urls``.

    The SDK's own documents carry the issuer as a pydantic URL, which gains a
    trailing slash the issuer does not have (RFC 8414 compares it exactly), and
    advertise only the secret-based client auth Vigil never issues. ``/revoke``
    is replaced so a public client need not send ``client_secret``.
    """
    urls = oauth_urls()
    issuer, resource = urls["issuer"], urls["resource"]
    provider = VigilOAuthProvider()
    routes = create_auth_routes(
        provider,
        AnyHttpUrl(issuer),
        client_registration_options=_REGISTRATION,
        revocation_options=_REVOCATION,
    )
    document = build_metadata(
        AnyHttpUrl(issuer), None, _REGISTRATION, _REVOCATION
    ).model_dump(mode="json", exclude_none=True)
    document.update(
        issuer=issuer,
        token_endpoint_auth_methods_supported=["none"],
        revocation_endpoint_auth_methods_supported=["none"],
        authorization_response_iss_parameter_supported=True,
    )
    metadata = OAuthMetadata.model_validate(document)
    routes = [r for r in routes if r.path not in (AS_METADATA, REVOCATION_PATH)]
    routes.append(_metadata_route(AS_METADATA, MetadataHandler(metadata)))
    routes.append(_revoke_route(provider))

    (prm_route,) = create_protected_resource_routes(
        AnyHttpUrl(resource), [AnyHttpUrl(issuer)], scopes_supported=[SCOPE]
    )
    prm = ProtectedResourceMetadata.model_validate(
        {
            "resource": resource,
            "authorization_servers": [issuer],
            "scopes_supported": [SCOPE],
            "resource_name": "Vigil",
        }
    )
    routes.append(
        _metadata_route(prm_route.path, ProtectedResourceMetadataHandler(prm))
    )
    return routes


# slowapi falls back to memory only inside its decorator; this does the same here.
_FALLBACK = FixedWindowRateLimiter(MemoryStorage())


def _hit(rate, path: str, address: str) -> bool:
    """One request against the limit; True while under it."""
    try:
        return limiter.limiter.hit(rate, "oauth", path, address)
    except Exception:  # noqa: BLE001 - an unreachable store must not answer 500
        logger.warning("OAuth rate limit storage unavailable; limiting in memory")
        return _FALLBACK.hit(rate, "oauth", path, address)


def _json(status: int, body: dict, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=headers)


class OAuthSurface:
    """Answers the authorization server's paths; passes everything else on."""

    def __init__(self, app: ASGIApp):
        self.app = app
        self._router: Optional[Router] = None

    def _routes(self) -> Router:
        # Built on first use: the base URL is only known safe once OAuth is on.
        if self._router is None:
            # Its own 404: this runs outside the app's exception handlers, so
            # the Router's default (raising HTTPException) would answer 500.
            self._router = Router(
                routes=build_routes(),
                redirect_slashes=False,
                default=_json(404, {"detail": "Not Found"}),
            )
        return self._router

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not claims(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        if not is_oauth_enabled():
            await _json(404, {"detail": "Not Found"})(scope, receive, send)
            return

        path = scope["path"]
        address = client_host(Request(scope))
        rate = _LIMITS.get(path)
        if rate is not None and scope.get("method") != "OPTIONS":
            if not _hit(rate, path, address):
                await _json(
                    429,
                    {"error": "slow_down", "error_description": "Too many requests"},
                    {"Retry-After": "60"},
                )(scope, receive, send)
                return

        bound = request_address.set(address)
        try:
            await self._routes()(scope, receive, send)
        finally:
            request_address.reset(bound)
