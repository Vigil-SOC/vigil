import sys
from pathlib import Path

# Spawned as ``python3 core/integrations/<vendor>/tool.py`` with a narrowed env,
# so the repo root is not on sys.path and PYTHONPATH is not forwarded. Add it
# here so the ``core.*`` imports below resolve; otherwise they fail at spawn.
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import asyncio
import json
import logging
from urllib.parse import quote

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from core.integrations._base.aad_token import TokenError, fetch_client_credentials_token
from core.integrations._base.config import resolve
from core.integrations._base.tool_result import run_tool
from core.integrations.microsoft_defender.descriptor import MICROSOFT_DEFENDER

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def odata_string(value: str) -> str:
    """An OData string literal: a quote inside it is escaped by doubling it."""
    return "'" + value.replace("'", "''") + "'"


def refusal(resp: httpx.Response) -> dict:
    """Defender's own status and message for a call it refused."""
    try:
        err = resp.json().get("error") or {}
        message = err.get("message") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        message = None
    return {
        "error": message or resp.reason_phrase or "request refused",
        "status": resp.status_code,
    }


def action_summary(action: dict) -> dict:
    """A machine action's ID, type and status: Pending/InProgress is requested, not done."""
    return {
        "action_id": action.get("id"),
        "machine_id": action.get("machineId"),
        "type": action.get("type"),
        "status": action.get("status"),
        "created": action.get("creationDateTimeUtc"),
        "updated": action.get("lastUpdateDateTimeUtc"),
    }


def isolation_state(actions: list[dict]) -> dict:
    """Isolation state from a machine's actions; /machines carries no such field.

    The latest Isolate/Unisolate that succeeded sets the state; a newer one
    still Pending/InProgress is reported beside it as ``pending``.
    """
    acts = sorted(
        (a for a in actions if a.get("type") in ("Isolate", "Unisolate")),
        key=lambda a: a.get("creationDateTimeUtc") or "",
        reverse=True,
    )
    done = next((a for a in acts if a.get("status") == "Succeeded"), None)
    latest = acts[0] if acts else None
    pending = (
        latest if latest and latest.get("status") in ("Pending", "InProgress") else None
    )
    return {
        "state": "isolated" if done and done["type"] == "Isolate" else "not_isolated",
        "pending": action_summary(pending) if pending else None,
        "latest_action": action_summary(latest) if latest else None,
    }


def get_token():
    """Access token, or None when credentials are not configured.

    Raises ``TokenError`` when they are configured but the exchange fails.
    """
    config = resolve(MICROSOFT_DEFENDER)
    tenant = config.get("tenant_id")
    client_id = config.get("client_id")
    client_secret = config.get("client_secret")
    if not all([tenant, client_id, client_secret]):
        return None
    return fetch_client_credentials_token(
        "Microsoft Defender",
        tenant,
        client_id,
        client_secret,
        "https://api.securitycenter.microsoft.com/.default",
    )


async def handle_list_tools():
    return [
        types.Tool(
            name="mde_get_alerts",
            description="Get Microsoft Defender alerts",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
                "required": [],
            },
        ),
        types.Tool(
            name="mde_get_machine",
            description="Get machine info",
            inputSchema={
                "type": "object",
                "properties": {"machine_id": {"type": "string"}},
                "required": ["machine_id"],
            },
        ),
        types.Tool(
            name="mde_find_machine",
            description="Find machines by host name or IP: each match's machine "
            "ID, name and isolation state",
            inputSchema={
                "type": "object",
                "properties": {
                    "hostname": {"type": "string"},
                    "ip": {"type": "string"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="mde_get_machine_action",
            description="Read a machine action (e.g. an isolate or release): its "
            "type and status (Pending, InProgress, Succeeded, Failed...)",
            inputSchema={
                "type": "object",
                "properties": {"action_id": {"type": "string"}},
                "required": ["action_id"],
            },
        ),
        types.Tool(
            name="mde_release",
            description="Release a machine from isolation; answers the machine "
            "action Defender created",
            inputSchema={
                "type": "object",
                "properties": {
                    "machine_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["machine_id", "comment"],
            },
        ),
        types.Tool(
            name="mde_isolate",
            description="Isolate machine; answers the machine action Defender "
            "created, whose status says whether the isolation is done",
            inputSchema={
                "type": "object",
                "properties": {
                    "machine_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["machine_id", "comment"],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    try:
        token = get_token()
    except TokenError as exc:
        return result({"error": str(exc)})
    if not token:
        return result({"error": "Microsoft Defender not configured"})

    args = arguments or {}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = "https://api.securitycenter.microsoft.com/api"

    try:
        if name == "mde_get_alerts":
            resp = httpx.get(
                f"{base}/alerts",
                headers=headers,
                params={"$top": args.get("limit", 20)},
                timeout=30,
            )
            resp.raise_for_status()
            alerts = [
                {
                    "id": a.get("id"),
                    "title": a.get("title"),
                    "severity": a.get("severity"),
                    "status": a.get("status"),
                }
                for a in resp.json().get("value", [])
            ]
            return result({"count": len(alerts), "alerts": alerts})

        elif name == "mde_get_machine":
            mid = args.get("machine_id")
            if not mid:
                return result({"error": "machine_id required"})
            resp = httpx.get(f"{base}/machines/{mid}", headers=headers, timeout=30)
            resp.raise_for_status()
            return result({"machine": resp.json()})

        elif name == "mde_find_machine":
            clauses = []
            if host := (args.get("hostname") or "").lower():
                # Defender keeps the lowercase FQDN; an alert often has the short name.
                clauses.append(f"computerDnsName eq {odata_string(host)}")
                clauses.append(
                    f"startswith(computerDnsName,{odata_string(host + '.')})"
                )
            if ip := args.get("ip"):
                clauses.append(f"lastIpAddress eq {odata_string(ip)}")
                clauses.append(f"lastExternalIpAddress eq {odata_string(ip)}")
            if not clauses:
                return result({"error": "hostname or ip required"})
            resp = httpx.get(
                f"{base}/machines",
                headers=headers,
                params={"$filter": " or ".join(clauses)},
                timeout=30,
            )
            if resp.is_error:
                return result(refusal(resp))
            machines = []
            for m in resp.json().get("value", []):
                acts = httpx.get(
                    f"{base}/machineactions",
                    headers=headers,
                    params={
                        "$filter": f"machineId eq {odata_string(m.get('id') or '')}"
                    },
                    timeout=30,
                )
                if acts.is_error:
                    return result(refusal(acts))
                machines.append(
                    {
                        "machine_id": m.get("id"),
                        "name": m.get("computerDnsName"),
                        "last_ip": m.get("lastIpAddress"),
                        "external_ip": m.get("lastExternalIpAddress"),
                        "health": m.get("healthStatus"),
                        "isolation": isolation_state(acts.json().get("value", [])),
                    }
                )
            return result({"count": len(machines), "machines": machines})

        elif name == "mde_get_machine_action":
            aid = args.get("action_id")
            if not aid:
                return result({"error": "action_id required"})
            resp = httpx.get(
                f"{base}/machineactions/{quote(aid, safe='')}",
                headers=headers,
                timeout=30,
            )
            if resp.status_code == 404:
                return result({"error": "machine action not found", "status": 404})
            if resp.is_error:
                return result(refusal(resp))
            return result(action_summary(resp.json()))

        elif name in ("mde_isolate", "mde_release"):
            mid = args.get("machine_id")
            comment = args.get("comment")
            if not mid or not comment:
                return result({"error": "machine_id and comment required"})
            isolate = name == "mde_isolate"
            body = {"Comment": comment}
            if isolate:
                body["IsolationType"] = "Full"
            resp = httpx.post(
                f"{base}/machines/{quote(mid, safe='')}/"
                f"{'isolate' if isolate else 'unisolate'}",
                headers=headers,
                json=body,
                timeout=30,
            )
            if resp.status_code == 404:
                return result({"error": "machine not found", "status": 404})
            if resp.is_error:
                return result(refusal(resp))
            # Defender accepted a request; the action's status says when it is done.
            return result(action_summary(resp.json()))

        return result({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return result({"error": str(e)})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "microsoft-defender",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="microsoft-defender",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
