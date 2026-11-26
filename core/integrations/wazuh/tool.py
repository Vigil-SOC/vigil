"""Wazuh MCP tool server.

Read-only tools over the Wazuh manager's server API: loaded rules, agents, their
groups and log collection, and logtest of a sample event against the ruleset.
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


# A full page of agents (long names, OS strings, groups) measured ~360 chars each
# as compact JSON, so 40 stays well under the agent layer's 20 000-char result cap.
AGENTS_PAGE_MAX = 40


def result(data, compact=False):
    text = (
        json.dumps(data, separators=(",", ":"))
        if compact
        else json.dumps(data, indent=2)
    )
    return [types.TextContent(type="text", text=text)]


def _int_arg(value, default):
    """A non-negative int from a model's argument (an int or digit string)."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError
    n = int(value)
    if n < 0:
        raise ValueError
    return n


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
                        "type": ["array", "integer", "string"],
                        "items": {"type": ["integer", "string"]},
                        "description": "Only these rule ids: a list, one id, or '5716,5712'",
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
            description=(
                "List Wazuh agents (id, name, status, groups, OS, IP, version), one page "
                "at a time, with `total` matching agents. Filter by group, exact name or "
                "Wazuh's free-text search; page with offset/limit and read on from "
                "`next_offset`. Agent 000 is the manager. For group sizes, use "
                "wazuh_list_groups instead of paging through everything."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "description": "Only agents in this group, e.g. 'default'",
                    },
                    "name": {
                        "type": "string",
                        "description": "Only the agent with exactly this name",
                    },
                    "search": {
                        "type": "string",
                        "description": "Wazuh's substring search across agent fields (name, IP, OS, ...)",
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": AGENTS_PAGE_MAX,
                        "default": AGENTS_PAGE_MAX,
                    },
                },
            },
        ),
        types.Tool(
            name="wazuh_list_groups",
            description=(
                "Wazuh agent groups with how many agents each holds, plus the count of "
                "agents in no group. An agent in two groups counts in both. Agent 000 "
                "is named as the manager and not counted. Built from the agents this "
                "API user can read (agent:read), not the group list (group:read), so a "
                "group with no agents does not appear."
            ),
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
            try:
                offset = _int_arg(args.get("offset"), 0)
                limit = _int_arg(args.get("limit"), AGENTS_PAGE_MAX)
            except (TypeError, ValueError):
                return result(
                    {"error": "offset and limit must be non-negative integers"}
                )
            # Clamped here too: the schema maximum is advice to the model.
            limit = min(max(limit, 1), AGENTS_PAGE_MAX)
            agents, total = await svc.list_agents(
                group=args.get("group"),
                name=args.get("name"),
                search=args.get("search"),
                offset=offset,
                limit=limit,
            )
            page = {"total": total, "offset": offset, "limit": limit, "agents": agents}
            if agents and offset + len(agents) < total:
                page["next_offset"] = offset + len(agents)
            return result(page, compact=True)
        if name == "wazuh_list_groups":
            return result(await svc.group_counts())
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
