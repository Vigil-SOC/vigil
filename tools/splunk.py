import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# GH #84 PR-F follow-up: prefer secrets_manager over direct env reads so
# SPLUNK_* credentials can be rotated without editing .env. The MCP server
# spawns as a subprocess from the repo root, so we need to add backend/ to
# sys.path to find secrets_manager. If the import fails (e.g. the server is
# running outside the repo), we fall back to os.environ — the keyring /
# dotenv lookups just get skipped.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
if _BACKEND_DIR.is_dir() and str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

try:
    from secrets_manager import get_secret as _get_secret  # type: ignore
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
    "failed login": 'index=* (failed OR failure) (login OR logon) | stats count by src_ip, user | sort -count',
    "powershell": 'index=* sourcetype=WinEventLog:Security EventCode=4688 powershell.exe | table _time, Computer, User, CommandLine',
    "brute force": 'index=* (failed OR failure) login | stats count by src_ip | where count > 10',
    "c2 beacon": 'index=* sourcetype=firewall action=allowed | stats count by dest_ip, dest_port | where count > 100',
    "lateral movement": 'index=* (EventCode=4624 OR psexec) Logon_Type=3 | stats dc(dest) as hosts by user | where hosts > 5',
}


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_splunk_service():
    try:
        from services.splunk_service import SplunkService
        url = _read_credential("SPLUNK_URL")
        if not url:
            return None
        return SplunkService(
            server_url=url,
            username=_read_credential("SPLUNK_USERNAME"),
            password=_read_credential("SPLUNK_PASSWORD"),
            verify_ssl=(_read_credential("SPLUNK_VERIFY_SSL", "false") or "false").lower() == "true"
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
        spl = f'index=* {terms} | head 100'
    if indexes:
        spl = spl.replace("index=*", "index=" + " OR index=".join(indexes))
    return {"spl_query": spl, "pattern": pattern}


@server.list_tools()
async def handle_list_tools():
    return [
        types.Tool(name="splunk_generate_spl", description="Generate SPL from natural language",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string"}, "indexes": {"type": "array", "items": {"type": "string"}}
            }, "required": ["query"]}),
        types.Tool(name="splunk_execute", description="Execute SPL query",
            inputSchema={"type": "object", "properties": {
                "spl_query": {"type": "string"},
                "earliest": {"type": "string", "default": "-24h"},
                "max_results": {"type": "integer", "default": 100}
            }, "required": ["spl_query"]}),
        types.Tool(name="splunk_search_ip", description="Search events for IP",
            inputSchema={"type": "object", "properties": {
                "ip_address": {"type": "string"}, "hours": {"type": "integer", "default": 24}
            }, "required": ["ip_address"]}),
        types.Tool(name="splunk_search_host", description="Search events for hostname",
            inputSchema={"type": "object", "properties": {
                "hostname": {"type": "string"}, "hours": {"type": "integer", "default": 24}
            }, "required": ["hostname"]}),
        types.Tool(name="splunk_nl_search", description="Natural language search (generate + execute)",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string"}, "max_results": {"type": "integer", "default": 100}
            }, "required": ["query"]}),
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
            results = splunk.search(spl, args.get("earliest", "-24h"), "now", args.get("max_results", 100))
            return result({"success": True, "query": spl, "count": len(results or []), "results": results or []})
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
            results = splunk.search_by_ip(ip, args.get("hours", 24))
            return result({"success": True, "ip": ip, "count": len(results or []), "results": results or []})
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
            results = splunk.search_by_hostname(host, args.get("hours", 24))
            return result({"success": True, "hostname": host, "count": len(results or []), "results": results or []})
        except Exception as e:
            return result({"error": str(e)})
    
    elif name == "splunk_nl_search":
        query = args.get("query")
        if not query:
            return result({"error": "query required"})
        spl_result = generate_spl(query)
        splunk = get_splunk_service()
        if not splunk:
            return result({"error": "Splunk not configured", "generated_spl": spl_result})
        try:
            results = splunk.search(spl_result["spl_query"], "-24h", "now", args.get("max_results", 100))
            return result({
                "success": True, "query": query, "spl": spl_result["spl_query"],
                "pattern": spl_result["pattern"], "count": len(results or []), "results": results or []
            })
        except Exception as e:
            return result({"error": str(e), "spl": spl_result["spl_query"]})
    
    return result({"error": f"Unknown tool: {name}"})


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name="splunk", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={})
        ))


if __name__ == "__main__":
    asyncio.run(main())
