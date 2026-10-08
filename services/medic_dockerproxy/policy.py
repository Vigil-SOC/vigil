"""The path list and the raw request checks (C3 §4.3, SP2 §3).

The path is matched raw, segment by segment, the way the gateway does it:
anything ambiguous is refused, never normalised. The query is decoded once and
every value re-checked; the request sent to Docker is rebuilt from the parsed
values, never copied from the caller.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass

MAX_HEAD = 8192
MAX_FILTERS = 2048
# No '%', '+', ';', '#', '\\', ' ' or non-ASCII in a path, no empty segment.
PATH_RE = re.compile(r"(/[A-Za-z0-9_.-]+)+")
VERSION_RE = re.compile(r"v1\.[0-9]{1,2}")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")  # Docker's name/ID charset
HEADER_NAME_RE = re.compile(r"[A-Za-z0-9-]+")
REFUSED_HEADERS = {
    "transfer-encoding",
    "upgrade",
    "expect",
    "x-http-method-override",
    "x-http-method",
    "x-method-override",
}
VALUE = {
    "bool": re.compile(r"0|1|true|false"),
    "int": re.compile(r"[0-9]{1,6}"),
    "ts": re.compile(r"[0-9]{1,12}(\.[0-9]{1,9})?"),  # secs[.nanos], SP2 §2 row 1b
    "tail": re.compile(r"[0-9]{1,7}|all"),
    "filters": re.compile(rf".{{1,{MAX_FILTERS}}}", re.DOTALL),
}
EVENT_TYPES = ["container"]

# name -> (segments, allowed query names and their value kinds). `{id}` = ID_RE.
# `details` is never allowed on logs: it adds log-opt env attributes (SP2 §3).
ROUTES = {
    "ping": (("_ping",), {}),
    "list": (
        ("containers", "json"),
        {"all": "bool", "limit": "int", "filters": "filters"},
    ),
    "inspect": (("containers", "{id}", "json"), {}),
    "logs": (
        ("containers", "{id}", "logs"),
        {
            "follow": "bool",
            "stdout": "bool",
            "stderr": "bool",
            "timestamps": "bool",
            "since": "ts",
            "until": "ts",
            "tail": "tail",
        },
    ),
    "events": (("events",), {"since": "ts", "until": "ts", "filters": "filters"}),
}
STREAMING = {"ping", "logs", "events"}  # piped; the rest are projected


class Refuse(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


@dataclass(frozen=True)
class Request:
    route: str
    target: str  # what is sent to Docker, rebuilt from parsed parts


def _match(segments: list[str]) -> str | None:
    if segments and VERSION_RE.fullmatch(segments[0]):
        segments = segments[1:]
    for name, (want, _) in ROUTES.items():
        if len(want) == len(segments) and all(
            ID_RE.fullmatch(g) if w == "{id}" else w == g
            for w, g in zip(want, segments, strict=True)
        ):
            return name
    return None


def _filters(value: str) -> dict:
    """A Docker filters object: {name: [str, …]} or {name: {str: bool}}."""
    try:
        f = json.loads(value)
    except ValueError:
        raise Refuse(400, "bad_filters") from None
    if not isinstance(f, dict):
        raise Refuse(400, "bad_filters")
    for k, v in f.items():
        ok = isinstance(v, list) and all(isinstance(x, str) for x in v)
        ok = ok or (
            isinstance(v, dict)
            and all(isinstance(x, str) and isinstance(b, bool) for x, b in v.items())
        )
        if not (isinstance(k, str) and ok):
            raise Refuse(400, "bad_filters")
    return f


def check_target(target: str) -> Request:
    path, sep, query = target.partition("?")
    if not PATH_RE.fullmatch(path) or ".." in path:
        raise Refuse(400, "ambiguous_path")
    route = _match(path.split("/")[1:])
    if route is None:
        raise Refuse(403, "not_on_list")
    allowed = ROUTES[route][1]
    if "#" in query:
        raise Refuse(400, "ambiguous_query")
    try:
        pairs = urllib.parse.parse_qsl(
            query, keep_blank_values=True, strict_parsing=bool(sep), max_num_fields=10
        )
    except ValueError:
        raise Refuse(400, "ambiguous_query") from None
    seen: dict[str, str] = {}
    for k, v in pairs:
        if k not in allowed or k in seen or not VALUE[allowed[k]].fullmatch(v):
            raise Refuse(403, "query_not_allowed")
        seen[k] = v
    if "filters" in seen or route == "events":
        f = _filters(seen.get("filters", "{}"))
        if route == "events":
            f["type"] = EVENT_TYPES  # forced: container events only (SP2 §3)
        seen["filters"] = json.dumps(f, separators=(",", ":"), sort_keys=True)
    q = urllib.parse.urlencode(seen)
    return Request(route, path + ("?" + q if q else ""))


def check_head(head: bytes) -> Request:
    """Validate a raw request head (without the final CRLFCRLF)."""
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError:
        raise Refuse(400, "bad_head") from None
    line, *headers = text.split("\r\n")
    parts = line.split(" ")
    if len(parts) != 3 or parts[2] != "HTTP/1.1" or not line.isprintable():
        raise Refuse(400, "bad_request_line")
    method, target, _ = parts
    if method != "GET":
        raise Refuse(405, "method_not_allowed")
    hosts = 0
    for h in headers:
        name, colon, value = h.partition(":")
        if not colon or not HEADER_NAME_RE.fullmatch(name) or not value.isprintable():
            raise Refuse(400, "bad_header")
        name = name.lower()
        if name in REFUSED_HEADERS:
            raise Refuse(400, "header_not_allowed")
        if name == "content-length" and value.strip() != "0":
            raise Refuse(400, "get_with_body")
        hosts += name == "host"
    if hosts > 1 or sum(h.lower().startswith("content-length:") for h in headers) > 1:
        raise Refuse(400, "duplicate_header")
    return check_target(target)
