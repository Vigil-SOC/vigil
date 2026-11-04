"""Wazuh MCP tool server.

Read-only tools over the Wazuh manager's server API: loaded rules, agents and
their log collection, and logtest of a sample event against the ruleset.
"""

import sys
from pathlib import Path

# Spawned as ``python3 core/integrations/wazuh/tool.py`` with a narrowed env,
# so the repo root is not on sys.path; add it so the ``core.*`` imports resolve.
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import asyncio
import json
import logging

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from core.integrations._base.config import missing, resolve
from core.integrations._base.tool_result import run_tool
from core.integrations.wazuh.client import WazuhError, WazuhService
from core.integrations.wazuh.descriptor import WAZUH, WAZUH_REQUIRED

logger = logging.getLogger(__name__)
_wazuh_service = None


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_wazuh_service():
    """The service, or the names of the fields still missing."""
    global _wazuh_service
    if _wazuh_service is not None:
        return _wazuh_service, ()
    config = resolve(WAZUH)
    absent = missing(config, *WAZUH_REQUIRED)
    if absent:
        return None, absent
    # resolve() returns every declared field; an unset verify_ssl means verify.
    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    _wazuh_service = WazuhService(
        api_url=config["api_url"],
        username=config["username"],
        password=config["password"],
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
    )
    return _wazuh_service, ()


def _sids(value):
    """``if_sid`` / ``if_matched_sid`` as a list; the manager sends a comma string."""
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value]
    return [s.strip() for s in str(value).replace(" ", ",").split(",") if s.strip()]


def _rule(r: dict) -> dict:
    details = r.get("details") or {}
    return {
        "id": r.get("id"),
        "level": r.get("level"),
        "description": r.get("description"),
        "groups": r.get("groups", []),
        "if_sid": _sids(details.get("if_sid")),
        "if_matched_sid": _sids(details.get("if_matched_sid")),
        "mitre": r.get("mitre", []),
        "filename": r.get("filename"),
        "relative_dirname": r.get("relative_dirname"),
        "status": r.get("status"),
    }


# ------------------------------------------------------------------
# Tool listing
# ------------------------------------------------------------------


async def handle_list_tools():
    return [
        types.Tool(
            name="wazuh_list_rules",
            description=(
                "List the rules loaded on the Wazuh manager: id, level, description, "
                "groups, if_sid / if_matched_sid parents, MITRE technique ids, file "
                "and status. Returns every matching rule, not a page."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Only these rule ids",
                    },
                    "level": {
                        "type": "string",
                        "description": "A level ('10') or range ('7-15')",
                    },
                    "group": {
                        "type": "string",
                        "description": "Rule group, e.g. 'sshd'",
                    },
                    "search": {"type": "string", "description": "Free-text search"},
                    "status": {
                        "type": "string",
                        "enum": ["enabled", "disabled", "all"],
                    },
                },
            },
        ),
        types.Tool(
            name="wazuh_list_agents",
            description="List Wazuh agents (id, name, status, groups, OS). Agent 000 is the manager.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="wazuh_agent_log_collection",
            description=(
                "Read one Wazuh agent's active log collection: its logcollector "
                "localfile entries (files, commands, event channels and formats)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent id, e.g. '000' for the manager",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        types.Tool(
            name="wazuh_test_event",
            description=(
                "Run a sample log line through the manager's ruleset (logtest) and "
                "report which rule matched, its level, the decoder, and the full output. "
                "Nothing is stored; the logtest session is closed afterwards."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "event": {"type": "string", "description": "The raw log line"},
                    "log_format": {"type": "string", "default": "syslog"},
                    "location": {
                        "type": "string",
                        "description": "Source location the event claims, e.g. '/var/log/auth.log'",
                        "default": "vigil",
                    },
                },
                "required": ["event"],
            },
        ),
    ]


# ------------------------------------------------------------------
# Tool dispatch
# ------------------------------------------------------------------


async def handle_call_tool(name: str, arguments: dict | None):
    svc, absent = get_wazuh_service()
    if svc is None:
        return result(
            {"error": f"Wazuh is not configured: missing {', '.join(absent)}"}
        )
    args = arguments or {}
    try:
        if name == "wazuh_list_rules":
            rules, total = await svc.list_rules(
                rule_ids=args.get("rule_ids"),
                level=args.get("level"),
                group=args.get("group"),
                search=args.get("search"),
                status=args.get("status"),
            )
            return result({"total": total, "rules": [_rule(r) for r in rules]})
        if name == "wazuh_list_agents":
            agents = await svc.list_agents()
            return result({"total": len(agents), "agents": agents})
        if name == "wazuh_agent_log_collection":
            agent_id = str(args.get("agent_id") or "").strip()
            if not agent_id:
                return result({"error": "agent_id is required"})
            return result(
                {
                    "agent_id": agent_id,
                    "localfile": await svc.agent_log_collection(agent_id),
                }
            )
        if name == "wazuh_test_event":
            if not args.get("event"):
                return result({"error": "event is required"})
            data = await svc.test_event(
                args["event"],
                log_format=args.get("log_format") or "syslog",
                location=args.get("location") or "vigil",
            )
            output = data.get("output") or {}
            rule = output.get("rule") or {}
            return result(
                {
                    "matched": bool(rule),
                    "rule_id": rule.get("id"),
                    "rule_level": rule.get("level"),
                    "rule_description": rule.get("description"),
                    "decoder": (output.get("decoder") or {}).get("name"),
                    "alert": data.get("alert"),
                    "messages": data.get("messages", []),
                    "output": output,
                }
            )
    except WazuhError as exc:
        return result({"error": str(exc)})
    return result({"error": f"Unknown tool: {name}"})


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "wazuh",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="wazuh",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
