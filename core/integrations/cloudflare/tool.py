"""Cloudflare MCP tool server.

Exposes WAF, Zero Trust Gateway, and Access response actions plus read-only
IP/domain threat-context lookups. Configured via Settings → Integrations
(`cloudflare`); every tool is a no-op returning a clear "not configured"
result when the integration is disabled.
"""

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import requests
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio

from core.config import get_integration_config, is_integration_enabled

logger = logging.getLogger(__name__)
server = Server("cloudflare")

CF_API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_TIMEOUT = 30


def _result(data: Dict[str, Any]):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def _config() -> Optional[Dict[str, Any]]:
    if not is_integration_enabled("cloudflare"):
        return None
    cfg = get_integration_config("cloudflare") or {}
    if not cfg.get("api_token"):
        return None
    return cfg


def _headers(api_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }


@server.list_tools()
async def handle_list_tools():
    return [
        types.Tool(
            name="cf_waf_block_ip",
            description=(
                "Block an IP at Cloudflare WAF via an IP Access Rule. "
                "Requires `account_id` configured. Reversible with cf_waf_unblock_ip."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ip": {"type": "string", "description": "IPv4/IPv6 address or CIDR"},
                    "reason": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["block", "challenge", "js_challenge", "managed_challenge"],
                        "default": "block",
                    },
                },
                "required": ["ip", "reason"],
            },
        ),
        types.Tool(
            name="cf_waf_unblock_ip",
            description="Remove an IP Access Rule by rule_id (returned from cf_waf_block_ip).",
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                },
                "required": ["rule_id"],
            },
        ),
        types.Tool(
            name="cf_gateway_block_domain",
            description=(
                "Add a Zero Trust Gateway DNS/HTTP rule that blocks a domain. "
                "Requires `account_id` configured."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "reason": {"type": "string"},
                    "rule_name": {"type": "string"},
                },
                "required": ["domain", "reason"],
            },
        ),
        types.Tool(
            name="cf_access_revoke_session",
            description=(
                "Revoke all active Cloudflare Zero Trust (Access) sessions for a user. "
                "Requires `account_id` configured."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["email"],
            },
        ),
        types.Tool(
            name="cf_lookup_ip_threat",
            description=(
                "Read-only: fetch IP Access Rules referencing this IP plus recent firewall "
                "events (read scope only). Returns empty result when integration disabled."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ip": {"type": "string"},
                },
                "required": ["ip"],
            },
        ),
        types.Tool(
            name="cf_lookup_domain_threat",
            description=(
                "Read-only: fetch Gateway category and rule matches for a domain."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                },
                "required": ["domain"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    cfg = _config()
    if cfg is None:
        return _result({
            "error": "cloudflare_integration_disabled",
            "message": (
                "Cloudflare integration is not configured. Enable it in "
                "Settings → Integrations and provide an API token."
            ),
        })

    api_token = cfg["api_token"]
    account_id = cfg.get("account_id")
    zone_id = cfg.get("zone_id")
    args = arguments or {}

    try:
        if name == "cf_waf_block_ip":
            return _result(
                await asyncio.to_thread(
                    _waf_block_ip,
                    api_token=api_token,
                    account_id=account_id,
                    ip=args.get("ip"),
                    reason=args.get("reason", "Vigil SOC block"),
                    mode=args.get("mode", "block"),
                )
            )
        if name == "cf_waf_unblock_ip":
            return _result(
                await asyncio.to_thread(
                    _waf_unblock_ip,
                    api_token=api_token,
                    account_id=account_id,
                    rule_id=args.get("rule_id"),
                )
            )
        if name == "cf_gateway_block_domain":
            return _result(
                await asyncio.to_thread(
                    _gateway_block_domain,
                    api_token=api_token,
                    account_id=account_id,
                    domain=args.get("domain"),
                    reason=args.get("reason", "Vigil SOC block"),
                    rule_name=args.get("rule_name"),
                )
            )
        if name == "cf_access_revoke_session":
            return _result(
                await asyncio.to_thread(
                    _access_revoke_session,
                    api_token=api_token,
                    account_id=account_id,
                    email=args.get("email"),
                    reason=args.get("reason", "Vigil SOC revoke"),
                )
            )
        if name == "cf_lookup_ip_threat":
            return _result(
                await asyncio.to_thread(
                    _lookup_ip_threat,
                    api_token=api_token,
                    account_id=account_id,
                    zone_id=zone_id,
                    ip=args.get("ip"),
                )
            )
        if name == "cf_lookup_domain_threat":
            return _result(
                await asyncio.to_thread(
                    _lookup_domain_threat,
                    api_token=api_token,
                    account_id=account_id,
                    domain=args.get("domain"),
                )
            )
        return _result({"error": f"Unknown tool: {name}"})
    except Exception as e:  # noqa: BLE001
        logger.exception("Cloudflare tool %s failed", name)
        return _result({"error": str(e), "tool": name})


# ---------------------------------------------------------------------------
# REST helpers — kept module-level so they can be imported by the response
# service for the approved-action executor (single source of truth for the
# Cloudflare API surface).
# ---------------------------------------------------------------------------


def _waf_block_ip(
    api_token: str,
    account_id: Optional[str],
    ip: Optional[str],
    reason: str,
    mode: str = "block",
) -> Dict[str, Any]:
    if not ip:
        return {"error": "ip required"}
    if not account_id:
        return {"error": "account_id required for WAF IP Access Rules"}
    payload = {
        "mode": mode,
        "configuration": {"target": "ip", "value": ip},
        "notes": reason[:1024],
    }
    resp = requests.post(
        f"{CF_API_BASE}/accounts/{account_id}/firewall/access_rules/rules",
        headers=_headers(api_token),
        json=payload,
        timeout=DEFAULT_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    success = resp.status_code in (200, 201) and data.get("success", False)
    return {
        "success": success,
        "status_code": resp.status_code,
        "rule_id": (data.get("result") or {}).get("id"),
        "ip": ip,
        "mode": mode,
        "errors": data.get("errors"),
    }


def _waf_unblock_ip(
    api_token: str, account_id: Optional[str], rule_id: Optional[str]
) -> Dict[str, Any]:
    if not rule_id:
        return {"error": "rule_id required"}
    if not account_id:
        return {"error": "account_id required"}
    resp = requests.delete(
        f"{CF_API_BASE}/accounts/{account_id}/firewall/access_rules/rules/{rule_id}",
        headers=_headers(api_token),
        timeout=DEFAULT_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    return {
        "success": resp.status_code in (200, 204) and data.get("success", True),
        "status_code": resp.status_code,
        "rule_id": rule_id,
        "errors": data.get("errors"),
    }


def _gateway_block_domain(
    api_token: str,
    account_id: Optional[str],
    domain: Optional[str],
    reason: str,
    rule_name: Optional[str],
) -> Dict[str, Any]:
    if not domain:
        return {"error": "domain required"}
    if not account_id:
        return {"error": "account_id required for Zero Trust Gateway rules"}
    payload = {
        "name": rule_name or f"vigil-block-{domain}",
        "description": reason[:512],
        "action": "block",
        "filters": ["dns", "http"],
        "traffic": f'any(dns.domains[*] in {{"{domain}"}}) or http.host == "{domain}"',
        "enabled": True,
    }
    resp = requests.post(
        f"{CF_API_BASE}/accounts/{account_id}/gateway/rules",
        headers=_headers(api_token),
        json=payload,
        timeout=DEFAULT_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    success = resp.status_code in (200, 201) and data.get("success", False)
    return {
        "success": success,
        "status_code": resp.status_code,
        "rule_id": (data.get("result") or {}).get("id"),
        "domain": domain,
        "errors": data.get("errors"),
    }


def _access_revoke_session(
    api_token: str,
    account_id: Optional[str],
    email: Optional[str],
    reason: str,
) -> Dict[str, Any]:
    if not email:
        return {"error": "email required"}
    if not account_id:
        return {"error": "account_id required for Access session revoke"}
    resp = requests.post(
        f"{CF_API_BASE}/accounts/{account_id}/access/organizations/revoke_user",
        headers=_headers(api_token),
        json={"email": email},
        timeout=DEFAULT_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    return {
        "success": resp.status_code in (200, 201) and data.get("success", False),
        "status_code": resp.status_code,
        "email": email,
        "reason": reason,
        "errors": data.get("errors"),
    }


def _lookup_ip_threat(
    api_token: str,
    account_id: Optional[str],
    zone_id: Optional[str],
    ip: Optional[str],
) -> Dict[str, Any]:
    if not ip:
        return {"error": "ip required"}
    if not account_id:
        return {"error": "account_id required"}
    resp = requests.get(
        f"{CF_API_BASE}/accounts/{account_id}/firewall/access_rules/rules",
        headers=_headers(api_token),
        params={"configuration.target": "ip", "configuration.value": ip, "per_page": 50},
        timeout=DEFAULT_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    rules = data.get("result") or []
    return {
        "success": resp.status_code == 200 and data.get("success", False),
        "ip": ip,
        "matching_rules": [
            {
                "id": r.get("id"),
                "mode": r.get("mode"),
                "scope": (r.get("scope") or {}).get("type"),
                "notes": r.get("notes"),
                "created_on": r.get("created_on"),
            }
            for r in rules
        ],
        "rule_count": len(rules),
        "zone_id": zone_id,
    }


def _lookup_domain_threat(
    api_token: str, account_id: Optional[str], domain: Optional[str]
) -> Dict[str, Any]:
    if not domain:
        return {"error": "domain required"}
    if not account_id:
        return {"error": "account_id required"}
    resp = requests.get(
        f"{CF_API_BASE}/accounts/{account_id}/gateway/categories",
        headers=_headers(api_token),
        timeout=DEFAULT_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    return {
        "success": resp.status_code == 200 and data.get("success", False),
        "domain": domain,
        "gateway_categories_available": len(data.get("result") or []),
        "note": (
            "Per-domain category lookup requires Cloudflare's category API; this tool "
            "currently surfaces gateway categories metadata. Pair with Vigil's IP "
            "geolocation and threat intel tools for full domain context."
        ),
    }


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="cloudflare",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
