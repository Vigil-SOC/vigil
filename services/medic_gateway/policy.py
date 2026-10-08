"""The two allow lists and the raw request checks (C3 §4.6, A3-2, SP1 §2-5).

Every byte of the request-target is checked against a narrow character set and an
exact list. Anything ambiguous is refused, never normalised, and the target sent
upstream is rebuilt from the list entry, never copied from the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_TARGET = 2048
MAX_POST_BODY = 4096  # X2: feedback bodies are at most 4 KB
# No '%', '+', ';', '#', '\\', '.', ' ' or non-ASCII in a path; no '%', '+', ';',
# '/', '?' in a query. '.' is allowed in a query only for timestamps.
PATH_RE = re.compile(r"/[A-Za-z0-9_\-/]*")
QUERY_RE = re.compile(r"[A-Za-z0-9_\-=&:.]+")
OVERRIDE_HEADERS = ("x-http-method-override", "x-http-method", "x-method-override")
NEVER_PREFIXES = ("/api/auth",)  # K1 T-35: never, whatever a list says


@dataclass(frozen=True)
class Route:
    """One allowed operation. `path` may hold `{name}` segments matched by `segs`."""

    method: str
    path: str
    query: dict = field(default_factory=dict)  # name -> (value regex, max repeats)
    segs: dict = field(default_factory=dict)  # path parameter -> regex
    required: tuple = ()  # query names that must be present
    auth: bool = True  # outbound: send the gateway's Viewer token

    def match_path(self, path: str) -> bool:
        want, got = self.path.split("/"), path.split("/")
        if len(want) != len(got):
            return False
        for w, g in zip(want, got, strict=True):
            if w.startswith("{"):
                if not re.fullmatch(self.segs[w[1:-1]], g):
                    return False
            elif w != g:
                return False
        return True


LIMIT = ("[1-9][0-9]{0,2}", 1)

# Outbound: SP1 §2, decision 1 (a'), Craig 2026-10-07. D6 owns the final list.
OUTBOUND = [
    Route("GET", "/api/health", auth=False),
    Route("GET", "/api/health/ready", auth=False),
    Route("GET", "/api/federation/sources"),
    Route("GET", "/api/federation/health"),
    Route("GET", "/api/triage"),
    Route("GET", "/api/orchestrator/status"),
    Route(
        "GET",
        "/api/v1/findings",
        {"data_source": ("probe", 1), "limit": LIMIT},
        required=("data_source",),
    ),
    Route("GET", "/api/v1/approvals/pending"),
    Route(
        "GET",
        "/api/v1/approvals",
        {"status": ("failed|pending", 1), "limit": LIMIT},
        required=("status",),
    ),
    Route(
        "GET",
        "/api/v1/agent-runs",
        {"status": ("failed|running|queued", 1), "limit": LIMIT},
        required=("status",),
    ),
    Route("GET", "/api/kafka/status"),
    Route("GET", "/api/llm/providers"),
    Route("GET", "/api/services/ollama/status"),  # L-4.d: ping + process check only
    Route("GET", "/api/webhooks/darktrace/health", auth=False),  # 404 = not installed
    Route("GET", "/api/webhooks/cloudflare/cloudy/health", auth=False),
]

# Inbound: exactly the operations in medic-api.openapi.yaml (X2); a test pins it.
TS = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z"
INC = r"inc_[0-9A-Za-z]{8,32}"
SEQ = r"0|[1-9][0-9]{0,18}"
CURSOR = ("[A-Za-z0-9_-]{1,256}", 1)
BOOL = ("true|false", 1)
TYPES = (
    "incident_opened|incident_updated|incident_resolved|feedback|adjudication"
    "|anchor|store_reset|pack_event"
)
INBOUND = [
    Route("GET", "/v1/status"),
    Route(
        "GET",
        "/v1/incidents",
        {
            "state": ("open|resolved|all", 1),
            "lane": ("[123]", 1),
            "subject": ("vigil|watcher", 1),
            "unrated": BOOL,
            "include_suppressed": BOOL,
            "opened_after": (TS, 1),
            "opened_before": (TS, 1),
            "cursor": CURSOR,
            "limit": ("[1-9][0-9]?|1[0-9]{2}|200", 1),
        },
    ),
    Route("GET", "/v1/incidents/{incident_id}", segs={"incident_id": INC}),
    Route("POST", "/v1/incidents/{incident_id}/feedback", segs={"incident_id": INC}),
    Route(
        "GET",
        "/v1/decisions",
        {
            "incident_id": (INC, 1),
            "type": (TYPES, 8),
            "from_seq": (SEQ, 1),
            "cursor": CURSOR,
            "limit": ("[1-9][0-9]?|100", 1),
        },
    ),
    Route("GET", "/v1/decisions/{seq}", segs={"seq": SEQ}),
    Route("GET", "/v1/sensors"),
    Route("GET", "/v1/pack"),
    Route("GET", "/v1/export", {"from": (TS, 1), "to": (TS, 1)}),
]


class Reject(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def check_routes(routes: list[Route]) -> None:
    for r in routes:
        if any(r.path == p or r.path.startswith(p + "/") for p in NEVER_PREFIXES):
            raise ValueError(f"{r.path} may never be listed")


def check_target(method: str, target: str, routes: list[Route]) -> tuple[Route, str]:
    """Validate one raw request-target. Returns (route, target to send) or raises."""
    if len(target) > MAX_TARGET:
        raise Reject(414, "target_too_long")
    path, sep, query = target.partition("?")
    if not PATH_RE.fullmatch(path) or "//" in path:
        raise Reject(400, "ambiguous_path")  # %, ;, #, ., \, +, absolute-form, '*'
    if path.endswith("/") and path != "/":
        raise Reject(400, "ambiguous_path")
    if any(path == p or path.startswith(p + "/") for p in NEVER_PREFIXES):
        raise Reject(403, "forbidden_path")
    on_path = [r for r in routes if r.match_path(path)]
    if not on_path:
        raise Reject(403, "not_on_list")
    route = next((r for r in on_path if r.method == method), None)
    if route is None:
        raise Reject(405, "method_not_allowed")
    if sep and not QUERY_RE.fullmatch(query):
        raise Reject(400, "ambiguous_query")
    counts: dict[str, int] = {}
    for part in query.split("&") if sep else []:
        name, eq, value = part.partition("=")
        rule = route.query.get(name)
        if not eq or rule is None or not re.fullmatch(rule[0], value):
            raise Reject(400, "query_not_allowed")
        counts[name] = counts.get(name, 0) + 1
        if counts[name] > rule[1]:
            raise Reject(400, "query_repeated")
    if any(n not in counts for n in route.required):
        raise Reject(400, "query_required")
    return route, path + (("?" + query) if sep else "")


def check_headers(method: str, headers) -> None:
    """Framing and override headers are refused outright: stripping hides smuggling."""
    if len(headers.get_all("Host") or []) > 1:
        raise Reject(400, "duplicate_host")
    if headers.get("Transfer-Encoding") is not None:
        raise Reject(400, "transfer_encoding")
    if any(headers.get(h) is not None for h in OVERRIDE_HEADERS):
        raise Reject(400, "method_override")
    cl = headers.get_all("Content-Length") or []
    if len(cl) > 1:
        raise Reject(400, "bad_content_length")
    if method == "GET" and cl and cl[0].strip() != "0":
        raise Reject(400, "get_with_body")
    if method == "POST":
        if len(cl) != 1 or not cl[0].isdigit():
            raise Reject(400, "bad_content_length")
        if int(cl[0]) > MAX_POST_BODY:
            raise Reject(413, "body_too_large")
        if (headers.get("Content-Type") or "").split(";")[
            0
        ].strip() != "application/json":
            raise Reject(415, "content_type")
