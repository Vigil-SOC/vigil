"""Raw-path bypass cases (C3 check 10), copied from the SP1 spike; S5 additions last.

Each case is one raw HTTP/1.1 request as it would arrive at the gateway from
Medic. `gateway` is what the gateway must do; `backend` is what Vigil's real
stack did with the same bytes (filled in by probe/probe_backend.py, at Vigil
7f52cd58 on uvicorn 0.54.0 + httptools 0.8.0 / h11 0.16.0, Starlette 1.6.0,
FastAPI 0.141.1, unauthenticated, so a protected route answers 401).

gateway values:
  ("forward", "<target sent to backend>")   exact match, forwarded verbatim
  ("reject", <status>)                      refused at the gateway, never forwarded
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    id: str
    target: bytes  # request-target exactly as sent on the wire
    gateway: tuple
    method: bytes = b"GET"
    headers: tuple = ()  # extra (name, value) byte pairs
    note: str = ""
    raw: bytes | None = field(
        default=None
    )  # whole request, when the line itself is the attack


F = "forward"
R = "reject"

CASES: list[Case] = [
    # --- the baseline: exact entries forward unchanged --------------------------
    Case("exact", b"/api/health", (F, "/api/health")),
    Case("exact-ready", b"/api/health/ready", (F, "/api/health/ready")),
    Case("exact-protected", b"/api/federation/sources", (F, "/api/federation/sources")),
    Case(
        "exact-query-allowed",
        b"/api/v1/findings?data_source=probe",
        (F, "/api/v1/findings?data_source=probe"),
    ),
    Case(
        "exact-ollama",
        b"/api/services/ollama/status",
        (F, "/api/services/ollama/status"),
    ),
    Case(
        "exact-webhook-health",
        b"/api/webhooks/darktrace/health",
        (F, "/api/webhooks/darktrace/health"),
    ),
    Case(
        "services-other-name",
        b"/api/services/splunk/status",
        (R, 403),
        note="only the ollama entry is listed; splunk returns a password string",
    ),
    Case("services-docker-list", b"/api/services", (R, 403)),
    Case(
        "storage-status",
        b"/api/storage/status",
        (R, 403),
        note="writes to the DB on every GET",
    ),
    Case(
        "analytics-cost",
        b"/api/analytics/cost?time_range=24h",
        (R, 403),
        note="sync Bifrost call blocks a backend worker up to 5 s",
    ),
    # --- slashes -----------------------------------------------------------------
    Case(
        "trailing-slash",
        b"/api/health/",
        (R, 400),
        note="backend 307-redirects to /api/health",
    ),
    Case("double-slash-lead", b"//api/health", (R, 400)),
    Case("double-slash-mid", b"/api//health", (R, 400)),
    Case("backslash", b"/api\\health", (R, 400)),
    # --- dot segments --------------------------------------------------------------
    Case("dot", b"/api/./health", (R, 400)),
    Case("dotdot", b"/api/x/../health", (R, 400)),
    Case(
        "dotdot-escape-to-auth", b"/api/health/../auth/password-reset/confirm", (R, 400)
    ),
    Case("trailing-dot", b"/api/health/.", (R, 400)),
    # --- percent-encoding: any % in the path is ambiguous -> reject ------------------
    Case(
        "enc-slash",
        b"/api%2Fhealth",
        (R, 400),
        note="uvicorn decodes %2F into / in scope.path",
    ),
    Case("enc-slash-lower", b"/api%2fhealth", (R, 400)),
    Case("enc-letter", b"/api/%68ealth", (R, 400), note="decodes to /api/health"),
    Case("enc-dot", b"/api/%2e/health", (R, 400)),
    Case("enc-dotdot", b"/api/x/%2e%2e/health", (R, 400)),
    Case("double-enc-slash", b"/api%252Fhealth", (R, 400)),
    Case("enc-null", b"/api/health%00", (R, 400)),
    Case("enc-auth", b"/api/%61uth/login", (R, 400)),
    Case("overlong-utf8", b"/api/%c0%afhealth", (R, 400)),
    # --- case, params, fragment, odd bytes ------------------------------------------
    Case("upper-case", b"/API/health", (R, 403), note="not on the list"),
    Case("mixed-case", b"/api/Health", (R, 403)),
    Case("semicolon-param", b"/api/health;x=1", (R, 400)),
    Case("semicolon-jsessionid", b"/api/federation/sources;jsessionid=a", (R, 400)),
    Case("fragment", b"/api/health#frag", (R, 400)),
    Case("raw-utf8", "/api/héalth".encode(), (R, 400)),
    Case("plus", b"/api/health+", (R, 400)),
    Case("tab-in-path", b"/api/health\tx", (R, 400)),
    # --- query strings ----------------------------------------------------------------
    Case("query-on-noquery-path", b"/api/health?x=1", (R, 400)),
    Case("empty-query", b"/api/health?", (R, 400)),
    Case(
        "query-unknown-param",
        b"/api/v1/findings?data_source=probe&limit=100000",
        (R, 400),
    ),
    Case(
        "query-dup-param",
        b"/api/v1/findings?data_source=probe&data_source=splunk",
        (R, 400),
    ),
    Case("query-wrong-value", b"/api/v1/findings?data_source=splunk", (R, 400)),
    Case("query-encoded-value", b"/api/v1/findings?data_source=%70robe", (R, 400)),
    Case("query-semicolon-sep", b"/api/v1/findings?data_source=probe;x=1", (R, 400)),
    Case("query-path-in-query", b"/api/health?/../auth/login", (R, 400)),
    # --- request-target forms ---------------------------------------------------------
    Case("absolute-form", b"http://backend:6987/api/health", (R, 400)),
    Case("absolute-form-other-host", b"http://evil.example/api/health", (R, 400)),
    Case("authority-form", b"backend:6987", (R, 400), method=b"CONNECT"),
    Case("asterisk-form", b"*", (R, 400), method=b"OPTIONS"),
    Case("no-leading-slash", b"api/health", (R, 400)),
    # --- context path -------------------------------------------------------------------
    Case(
        "ctx-prefix-from-caller",
        b"/vigil/api/health",
        (R, 403),
        note="the gateway adds VIGIL_CONTEXT_PATH itself; callers never send it",
    ),
    # --- size -----------------------------------------------------------------------------
    Case("oversized-path", b"/api/health/" + b"a" * 9000, (R, 414)),
    Case(
        "oversized-query",
        b"/api/v1/findings?data_source=probe&" + b"a=b&" * 3000,
        (R, 414),
    ),
    # --- off-list and forbidden -------------------------------------------------------------
    Case("off-list", b"/api/settings", (R, 403)),
    Case("auth-login", b"/api/auth/login", (R, 403)),
    Case(
        "auth-reset-confirm",
        b"/api/auth/password-reset/confirm",
        (R, 403),
        method=b"POST",
        note="C3 check 10/11 named case",
    ),
    Case("auth-me", b"/api/auth/me", (R, 403)),
    Case(
        "side-effect-insights",
        b"/api/analytics/insights",
        (R, 403),
        note="GET starts an LLM regeneration (C3 4.6)",
    ),
    Case(
        "side-effect-mcp",
        b"/api/mcp/connections/status",
        (R, 403),
        note="GET reconnects",
    ),
    Case("openapi", b"/openapi.json", (R, 400), note="'.' is outside the path charset"),
    Case("docs", b"/docs", (R, 403)),
    Case("metrics", b"/metrics", (R, 403)),
    Case("mcp-surface", b"/mcp", (R, 403)),
    Case("internal", b"/internal/health", (R, 403)),
    # --- methods ------------------------------------------------------------------------------
    Case("method-head", b"/api/health", (R, 405), method=b"HEAD"),
    Case("method-options", b"/api/health", (R, 405), method=b"OPTIONS"),
    Case("method-post", b"/api/federation/sources", (R, 405), method=b"POST"),
    Case("method-put", b"/api/federation/sources", (R, 405), method=b"PUT"),
    Case("method-delete", b"/api/federation/sources", (R, 405), method=b"DELETE"),
    Case("method-lowercase", b"/api/health", (R, 405), method=b"get"),
    Case(
        "method-override-header",
        b"/api/federation/sources",
        (R, 400),
        headers=((b"X-HTTP-Method-Override", b"DELETE"),),
    ),
    Case("method-override-query", b"/api/federation/sources?_method=DELETE", (R, 400)),
    # --- framing / smuggling ------------------------------------------------------------------
    Case(
        "get-with-body",
        b"/api/health",
        (R, 400),
        headers=((b"Content-Length", b"5"),),
        note="GET carries no body through the gateway",
    ),
    Case(
        "get-with-te",
        b"/api/health",
        (R, 400),
        headers=((b"Transfer-Encoding", b"chunked"),),
    ),
    Case(
        "cl-and-te",
        b"/api/health",
        (R, 400),
        headers=((b"Content-Length", b"0"), (b"Transfer-Encoding", b"chunked")),
    ),
    Case(
        "http10",
        b"/api/health",
        (R, 400),
        raw=b"GET /api/health HTTP/1.0\r\n\r\n",
        note="no Host; gateway speaks 1.1 only",
    ),
    Case(
        "dup-host",
        b"/api/health",
        (R, 400),
        headers=((b"Host", b"evil"),),
        note="second Host header on top of the client's",
    ),
    # --- Host tricks (the gateway always sends its own Host) ----------------------------------
    Case(
        "host-evil",
        b"/api/health",
        (F, "/api/health"),
        headers=(),
        note="Host is replaced, not trusted; see test_host_replaced",
    ),
    # --- added in S5 ---------------------------------------------------------------------------
    Case(
        "findings-without-filter",
        b"/api/v1/findings",
        (R, 400),
        note="data_source=probe is required: unfiltered findings are SOC data, not health",
    ),
    Case("approvals-without-status", b"/api/v1/approvals", (R, 400)),
    Case("agent-runs-without-status", b"/api/v1/agent-runs", (R, 400)),
    Case("query-value-only", b"/api/v1/findings?probe", (R, 400)),
    Case("query-empty-pair", b"/api/v1/findings?data_source=probe&", (R, 400)),
    Case(
        "query-optional-forwarded",
        b"/api/v1/findings?data_source=probe&limit=50",
        (F, "/api/v1/findings?data_source=probe&limit=50"),
    ),
    Case("gw-status-with-query", b"/_gw/status?x=1", (R, 403)),
    Case("gw-status-post", b"/_gw/status", (R, 403), method=b"POST"),
    Case("http09", b"/api/health", (R, 400), raw=b"GET /api/health\r\n\r\n"),
    Case(
        "http2-version",
        b"/api/health",
        (R, 400),
        raw=b"GET /api/health HTTP/2.0\r\nHost: g\r\n\r\n",
    ),
    Case(
        "double-space",
        b"/api/health",
        (R, 400),
        raw=b"GET  /api/health HTTP/1.1\r\nHost: g\r\n\r\n",
    ),
    Case(
        "dup-content-length",
        b"/api/health",
        (R, 400),
        headers=((b"Content-Length", b"0"), (b"Content-Length", b"0")),
    ),
]


def wire(case: Case, host: bytes = b"gateway") -> bytes:
    """The exact bytes a client would send for `case`."""
    if case.raw is not None:
        return case.raw
    lines = [case.method + b" " + case.target + b" HTTP/1.1", b"Host: " + host]
    lines += [k + b": " + v for k, v in case.headers]
    lines += [b"X-Case-Id: " + case.id.encode(), b"Connection: close"]
    body = (
        b"hello"
        if any(k.lower() == b"content-length" and v == b"5" for k, v in case.headers)
        else b""
    )
    if any(k.lower() == b"transfer-encoding" for k, v in case.headers):
        body = b"0\r\n\r\n"
    return b"\r\n".join(lines) + b"\r\n\r\n" + body
