import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# GH #84 PR-F follow-up: prefer the secrets layer over direct env reads so
# SPLUNK_* credentials can be rotated without editing .env. If the import
# fails (e.g. the server is running outside the repo), we fall back to
# os.environ — the keyring / dotenv lookups just get skipped.
try:
    from core.secrets import get_secret as _get_secret
except Exception:  # noqa: BLE001
    _get_secret = None  # type: ignore


def _read_credential(key: str, default: str | None = None) -> str | None:
    """Read a SPLUNK_* credential via secrets_manager, falling back to env."""
    if _get_secret is not None:
        value = _get_secret(key)
        if value is not None:
            return value
    return os.environ.get(key, default)


logger = logging.getLogger(__name__)
server = Server("splunk")

SPL_TEMPLATES = {
    "failed login": "index=* (failed OR failure) (login OR logon) | stats count by src_ip, user | sort -count",
    "powershell": "index=* sourcetype=WinEventLog:Security EventCode=4688 powershell.exe | table _time, Computer, User, CommandLine",
    "brute force": "index=* (failed OR failure) login | stats count by src_ip | where count > 10",
    "c2 beacon": "index=* sourcetype=firewall action=allowed | stats count by dest_ip, dest_port | where count > 100",
    "lateral movement": "index=* (EventCode=4624 OR psexec) Logon_Type=3 | stats dc(dest) as hosts by user | where hosts > 5",
}


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_splunk_service():
    try:
        from core.integrations.splunk.client import SplunkService

        url = _read_credential("SPLUNK_URL")
        if not url:
            return None
        return SplunkService(
            server_url=url,
            username=_read_credential("SPLUNK_USERNAME"),
            password=_read_credential("SPLUNK_PASSWORD"),
            verify_ssl=(
                _read_credential("SPLUNK_VERIFY_SSL", "false") or "false"
            ).lower()
            == "true",
        )
    except Exception:
        return None


def generate_spl(query: str, indexes=None):
    query_lower = query.lower()
    spl, pattern = None, "generic"
    for p, template in SPL_TEMPLATES.items():
        if p in query_lower:
            spl, pattern = template, p
            break
    if not spl:
        terms = query_lower.replace("show me", "").replace("find", "").strip()
        spl = f"index=* {terms} | head 100"
    if indexes:
        spl = spl.replace("index=*", "index=" + " OR index=".join(indexes))
    return {"spl_query": spl, "pattern": pattern}


# What this deployment actually holds, in the tool's own description. Told only
# "Execute SPL query", a model guesses an index and a time range, and a wrong guess
# comes back empty rather than wrong -- which reads as "no evidence" when it is
# really "no visibility". In the description rather than as a tool the lead must
# remember to call: there is no turn to spend on it and no way to skip it.
_SUMMARY_SPL = (
    "| tstats count, min(_time) AS earliest, max(_time) AS latest WHERE index=* "
    "BY index, sourcetype | sort - count | head 200"
)
_SUMMARY_ROWS = 200
# Sourcetypes named per index before the rest are counted rather than listed: this
# description is charged on every call the tool is offered on.
_TYPES_NAMED = 12
# Every accepted form was tried against this path rather than taken from Splunk's
# docs: the console's own MM/DD/YYYY:HH:MM:SS is silently empty through REST search.
_WHY_THE_SPAN_MATTERS = (
    "`earliest` defaults to 0, which is all time, so a query reaches the whole span "
    "above unless you narrow it deliberately. Empty results against a span you did "
    "narrow are a gap in what you looked at, not an absence of evidence. This used to "
    "default to -24h, and every telemetry query in a hunt over a 2018 dataset came "
    "back empty while reading as though nothing was there.\n"
    "`earliest` takes a relative offset (-15y), an ISO 8601 timestamp "
    "(2018-08-19T00:00:00) or an epoch second. It does NOT take Splunk's console "
    "form (08/19/2018:00:00:00), which returns nothing here. There is no `latest` "
    "parameter; the window always ends now, which covers any past span."
)
# Splunk's own all-time earliest: a narrower default is a silent zero on any data
# older than it.
_ALL_TIME = "0"

# search_by_ip and search_by_hostname build their window as -{hours}h, and their client
# is also the daemon's, so widening here leaves the daemon's polling window alone.
# ponytail: hours arithmetic, not an epoch -- the helpers take no earliest to pass.
_ALL_TIME_HOURS = 876_000

_summary_cache: Optional[str] = None


# tstats reads the index rather than the events, so its totals are right about which
# sourcetype is large and wrong about how large. Rounded and marked approximate,
# because relative magnitude is all a query needs and an exact number gets believed.
def _approx(count: int) -> str:
    if count >= 1_000_000:
        return f"~{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"~{count / 1_000:.0f}k"
    return str(count)


def _day(value: object) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


# Grouped by index, with the span stated once per index rather than once per
# sourcetype, which repeated one fact forty times on every call.
def _fold_by_index(rows: list) -> dict:
    held: dict = {}
    for row in rows:
        index = row.get("index")
        if not index:
            continue
        at = held.setdefault(index, {"count": 0, "types": [], "days": []})
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        at["count"] += count
        at["types"].append((count, str(row.get("sourcetype") or "?")))
        edges = (_day(row.get("earliest")), _day(row.get("latest")))
        at["days"] += [day for day in edges if day]
    return held


def _render_index(index: str, held: dict) -> str:
    days = sorted(held["days"])
    if not days:
        span = ""
    elif days[0] == days[-1]:
        span = days[0]
    else:
        span = f"{days[0]}..{days[-1]}"
    named = sorted(held["types"], reverse=True)[:_TYPES_NAMED]
    rest = len(held["types"]) - len(named)
    types = " ".join(f"{name}:{_approx(count)}" for count, name in named)
    more = f" +{rest} more sourcetypes" if rest > 0 else ""
    return f"  index={index} events={_approx(held['count'])} {span}\n    {types}{more}"


# Best effort: a summary that cannot be read leaves the plain description rather than
# failing list_tools. Only a summary that exists is cached -- this server starts before
# a credential may have been entered, and caching that first failure would leave every
# hunt for the life of the process with no index map. Retrying costs one tstats.
def _telemetry_summary() -> str:
    global _summary_cache
    if _summary_cache:
        return _summary_cache

    service = get_splunk_service()
    if service is None:
        return ""
    try:
        rows = service.search(
            _SUMMARY_SPL, earliest_time="-20y", max_count=_SUMMARY_ROWS
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not summarise Splunk telemetry: %s", exc)
        return ""

    by_index = _fold_by_index(list(rows or []))
    if not by_index:
        return ""

    ordered = sorted(by_index.items(), key=lambda pair: -pair[1]["count"])
    body = "\n".join(_render_index(index, held) for index, held in ordered)
    _summary_cache = (
        "\n\nWhat this deployment holds (UTC date span, then sourcetype:events, "
        "counts approximate):\n"
        f"{body}\n\n{_WHY_THE_SPAN_MATTERS}"
    )
    return _summary_cache


@server.list_tools()
async def handle_list_tools():
    telemetry = _telemetry_summary()
    return [
        types.Tool(
            name="splunk_generate_spl",
            description="Generate SPL from natural language",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "indexes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="splunk_execute",
            description="Execute SPL query" + telemetry,
            inputSchema={
                "type": "object",
                "properties": {
                    "spl_query": {"type": "string"},
                    "earliest": {"type": "string", "default": "0"},
                    "max_results": {"type": "integer", "default": 100},
                },
                "required": ["spl_query"],
            },
        ),
        types.Tool(
            name="splunk_search_ip",
            description="Search events for IP",
            inputSchema={
                "type": "object",
                "properties": {
                    "ip_address": {"type": "string"},
                    "hours": {"type": "integer"},
                },
                "required": ["ip_address"],
            },
        ),
        types.Tool(
            name="splunk_search_host",
            description="Search events for hostname",
            inputSchema={
                "type": "object",
                "properties": {
                    "hostname": {"type": "string"},
                    "hours": {"type": "integer"},
                },
                "required": ["hostname"],
            },
        ),
        types.Tool(
            name="splunk_nl_search",
            description="Natural language search (generate + execute). Takes no "
            "time range and reaches all time. Prefer splunk_execute, "
            "whose description lists what is held and which windows "
            "cover it, and which lets you narrow the span yourself.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 100},
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    args = arguments or {}

    if name == "splunk_generate_spl":
        query = args.get("query")
        if not query:
            return result({"error": "query required"})
        return result(generate_spl(query, args.get("indexes")))

    elif name == "splunk_execute":
        spl = args.get("spl_query")
        if not spl:
            return result({"error": "spl_query required"})
        splunk = get_splunk_service()
        if not splunk:
            return result({"error": "Splunk not configured", "spl": spl})
        try:
            results = splunk.search(
                spl,
                args.get("earliest", _ALL_TIME),
                "now",
                args.get("max_results", 100),
            )
            return result(
                {
                    "success": True,
                    "query": spl,
                    "count": len(results or []),
                    "results": results or [],
                }
            )
        except Exception as e:
            return result({"error": str(e), "query": spl})

    elif name == "splunk_search_ip":
        ip = args.get("ip_address")
        if not ip:
            return result({"error": "ip_address required"})
        splunk = get_splunk_service()
        if not splunk:
            return result({"error": "Splunk not configured"})
        try:
            results = splunk.search_by_ip(ip, args.get("hours") or _ALL_TIME_HOURS)
            return result(
                {
                    "success": True,
                    "ip": ip,
                    "count": len(results or []),
                    "results": results or [],
                }
            )
        except Exception as e:
            return result({"error": str(e)})

    elif name == "splunk_search_host":
        host = args.get("hostname")
        if not host:
            return result({"error": "hostname required"})
        splunk = get_splunk_service()
        if not splunk:
            return result({"error": "Splunk not configured"})
        try:
            results = splunk.search_by_hostname(
                host, args.get("hours") or _ALL_TIME_HOURS
            )
            return result(
                {
                    "success": True,
                    "hostname": host,
                    "count": len(results or []),
                    "results": results or [],
                }
            )
        except Exception as e:
            return result({"error": str(e)})

    elif name == "splunk_nl_search":
        query = args.get("query")
        if not query:
            return result({"error": "query required"})
        spl_result = generate_spl(query)
        splunk = get_splunk_service()
        if not splunk:
            return result(
                {"error": "Splunk not configured", "generated_spl": spl_result}
            )
        try:
            results = splunk.search(
                spl_result["spl_query"], _ALL_TIME, "now", args.get("max_results", 100)
            )
            return result(
                {
                    "success": True,
                    "query": query,
                    "spl": spl_result["spl_query"],
                    "pattern": spl_result["pattern"],
                    "count": len(results or []),
                    "results": results or [],
                }
            )
        except Exception as e:
            return result({"error": str(e), "spl": spl_result["spl_query"]})

    return result({"error": f"Unknown tool: {name}"})


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="splunk",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
