"""Name whose fault a federation fetch failure is, from the exception's type.

Only typed evidence counts (an HTTP status, an SDK error code, a socket or
timeout class). Message text is never read: it is vendor-controlled.
"""

from __future__ import annotations

import socket
from typing import Optional

import httpx

KINDS = ("auth", "network", "config", "quota", "upstream", "unknown")

_AWS_AUTH = {
    "UnrecognizedClientException",
    "InvalidSignatureException",
    "InvalidClientTokenId",
    "SignatureDoesNotMatch",
    "ExpiredToken",
    "ExpiredTokenException",
    "AccessDeniedException",
    "AccessDenied",
    "AuthFailure",
}
_AWS_QUOTA = {
    "ThrottlingException",
    "Throttling",
    "ThrottledException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
    "LimitExceededException",
}
_AWS_UPSTREAM = {"InternalFailure", "ServiceUnavailable", "InternalServerError"}


def _from_status(status: Optional[int]) -> Optional[str]:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "quota"
    if isinstance(status, int) and status >= 500:
        return "upstream"
    return None


def _one(exc: BaseException) -> Optional[str]:
    # A caller that knows the cause (a service that could not be built) tags it.
    tag = getattr(exc, "error_kind", None)
    if tag in KINDS:
        return tag
    if isinstance(exc, httpx.HTTPStatusError):
        return _from_status(exc.response.status_code)
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            socket.gaierror,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return "network"
    # SDKs are matched by class name so a missing one does not break this module.
    names = {c.__name__ for c in type(exc).__mro__}
    if "ClientError" in names and isinstance(getattr(exc, "response", None), dict):
        # botocore.exceptions.ClientError
        resp = exc.response
        code = (resp.get("Error") or {}).get("Code")
        status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code in _AWS_AUTH:
            return "auth"
        if code in _AWS_QUOTA:
            return "quota"
        if code in _AWS_UPSTREAM:
            return "upstream"
        return _from_status(status)
    if "ClientAuthenticationError" in names:  # azure.core.exceptions
        return "auth"
    if "HttpResponseError" in names:  # azure.core.exceptions
        return _from_status(getattr(exc, "status_code", None))
    if names & {"EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"}:
        return "network"  # botocore
    return None


def classify_failure(exc: BaseException) -> str:
    """One of :data:`KINDS`; walks ``__cause__``/``__context__`` for chained errors."""
    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        kind = _one(cur)
        if kind:
            return kind
        stack += [cur.__cause__, cur.__context__]
    return "unknown"
