import sys
from pathlib import Path

# Spawned as ``python3 core/integrations/<vendor>/tool.py`` with a narrowed env,
# so the repo root is not on sys.path and PYTHONPATH is not forwarded. Add it
# here so the ``core.*`` imports below resolve; otherwise they fail at spawn.
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import asyncio
import ipaddress
import json
import logging
import re
import xml.etree.ElementTree as ET  # nosec B405

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from core.integrations._base.config import missing, resolve
from core.integrations._base.tls import tls_verify
from core.integrations._base.tool_result import run_tool
from core.integrations.palo_alto.descriptor import PALO_ALTO

logger = logging.getLogger(__name__)


# One fixed tag. The operator's dynamic address group filters on it and a
# deny/drop rule uses that group, so a registration blocks without a commit.
BLOCK_TAG = "vigil-block"
VSYS = "vsys1"
_DENY_ACTIONS = frozenset(
    {"deny", "drop", "reset-client", "reset-server", "reset-both"}
)
# A filter term the tag alone satisfies: the tag, quoted or not.
_TAG_TERMS = {BLOCK_TAG, f"'{BLOCK_TAG}'", f'"{BLOCK_TAG}"'}
# PAN-OS refuses a repeat register/unregister with these entry messages; the
# firewall is then already in the asked-for state (pan-os-python reads them too).
_NO_OP = {"register": "already exists", "unregister": "does not exist"}


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def endpoint(config) -> tuple[str, object]:
    """The /api/ URL and httpx ``verify`` for the saved settings."""
    # The Settings form collects a hostname; PAN-OS is reached over HTTPS.
    host = str(config.get("hostname")).rstrip("/")
    url = host if host.startswith(("http://", "https://")) else f"https://{host}"
    # resolve() always returns every declared field, so a .get(k, True) default
    # would never fire — verify_ssl is present-but-None when unset.
    verify = True if config.get("verify_ssl") is None else config.get("verify_ssl")
    return f"{url}/api/", tls_verify(verify, config.get("ca_cert_path"))


def parse(resp: httpx.Response) -> tuple[ET.Element | None, str | None]:
    """``(root, None)`` for status="success", else ``(None, reason)``.

    The reason is PAN-OS's own <msg> text and per-entry messages, never the
    request: its params carry the API key.
    """
    try:
        root = ET.fromstring(resp.content)  # nosec B314 - the configured firewall
    except ET.ParseError:
        root = None
    if root is not None and resp.status_code == 200 and root.get("status") == "success":
        return root, None
    parts: list[str] = []
    if root is not None:
        for msg in root.iter("msg"):
            # A User-ID error nests a <uid-response>; its <version> is no reason.
            parts += [
                el.text.strip()
                for el in msg.iter()
                if el.tag != "version" and el.text and el.text.strip()
            ]
        parts += [e.get("message") for e in root.iter("entry") if e.get("message")]
    reason = "; ".join(dict.fromkeys(parts)) or "no reason given"
    return None, f"Palo Alto answered HTTP {resp.status_code}: {reason}"


def single_address(value) -> tuple[str | None, str | None]:
    """``(address, None)`` for exactly one IPv4/IPv6 address, else ``(None, why)``."""
    text = str(value or "").strip()
    if not text:
        return None, "ip is required: one IPv4 or IPv6 address"
    if "/" in text:
        return None, f"{text!r} is a network; give one IPv4 or IPv6 address"
    try:
        addr = ipaddress.ip_address(text)
        # A scope id (fe80::1%eth0) is free text and means nothing to PAN-OS.
        if getattr(addr, "scope_id", None):
            return None, f"{text!r} carries a scope id; give the address alone"
        return str(addr), None
    except ValueError:
        return (
            None,
            f"{text!r} is not an IPv4 or IPv6 address (hostnames are not accepted)",
        )


def _uid_message(op: str, ip: str) -> str:
    # No timeout attribute: the registration lasts until it is unregistered.
    return (
        "<uid-message><version>2.0</version><type>update</type><payload>"
        f'<{op}><entry ip="{ip}"><tag><member>{BLOCK_TAG}</member></tag></entry></{op}>'
        "</payload></uid-message>"
    )


def _tag_admits(expr: str) -> bool:
    """True when an address carrying only ``BLOCK_TAG`` matches the filter.

    Only plain or-lists are read; an and/not filter counts as not matching,
    so the Test fails rather than passing a group the tag alone cannot join.
    """
    words = re.split(r"\s+", expr.replace("(", " ").replace(")", " ").strip())
    if {"and", "not"} & {w.lower() for w in words}:
        return False
    return any(t.strip() in _TAG_TERMS for t in re.split(r"\s+or\s+", " ".join(words)))


def _no_op(resp: httpx.Response, op: str) -> bool:
    """True when every refused entry says it was already in the asked-for state."""
    try:
        root = ET.fromstring(resp.content)  # nosec B314 - the configured firewall
    except ET.ParseError:
        return False
    msgs = [e.get("message") or "" for e in root.iter("entry")]
    return bool(msgs) and all(_NO_OP[op] in m for m in msgs)


def block_setup(vsys: ET.Element | None, shared: ET.Element | None) -> tuple[bool, str]:
    """Whether the running config enforces ``BLOCK_TAG``, naming what is missing."""
    groups = [
        g.get("name")
        for scope in (vsys, shared)
        if scope is not None
        for ag in scope.iter("address-group")
        for g in ag.findall("entry")
        if _tag_admits(g.findtext("dynamic/filter") or "")
    ]
    if not groups:
        return False, f"no dynamic address group filter matches tag '{BLOCK_TAG}' alone"
    rules = vsys.findall(".//rulebase/security/rules/entry") if vsys is not None else []
    for rule in rules:
        # A negated side matches everything but the group, so it does not count.
        members = {
            m.text
            for side in ("source", "destination")
            if (rule.findtext(f"negate-{side}") or "no").strip() != "yes"
            for m in rule.findall(f"{side}/member")
        }
        if (
            (rule.findtext("action") or "").strip() in _DENY_ACTIONS
            and (rule.findtext("disabled") or "no").strip() != "yes"
            and members & set(groups)
        ):
            group = next(g for g in groups if g in members)
            return (
                True,
                f"rule {rule.get('name')!r} denies group {group!r} (tag '{BLOCK_TAG}')",
            )
    return False, (
        f"no enabled deny/drop security rule in {VSYS} uses "
        + ", ".join(repr(g) for g in groups)
    )


async def handle_list_tools():
    ip_schema = {
        "type": "object",
        "properties": {
            "ip": {
                "type": "string",
                "description": "One IPv4 or IPv6 address, not a network",
            },
            "reason": {"type": "string"},
        },
        "required": ["ip", "reason"],
    }
    return [
        types.Tool(
            name="pan_block_ip",
            description=(
                f"Register one IPv4/IPv6 address under tag '{BLOCK_TAG}' in {VSYS} "
                "via the User-ID API (no commit, no expiry). Traffic is denied only "
                "where a deny/drop rule uses a dynamic address group on that tag."
            ),
            inputSchema=ip_schema,
        ),
        types.Tool(
            name="pan_unblock_ip",
            description=f"Unregister one IPv4/IPv6 address from tag '{BLOCK_TAG}' in {VSYS}",
            inputSchema=ip_schema,
        ),
        types.Tool(
            name="pan_get_threats",
            description="Get threat logs",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
                "required": [],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    config = resolve(PALO_ALTO)
    api_key = config.get("api_key")
    if missing(config, "hostname", "api_key"):
        return result({"error": "Palo Alto not configured"})

    api, verify = endpoint(config)
    args = arguments or {}

    try:
        if name in ("pan_block_ip", "pan_unblock_ip"):
            ip, why = single_address(args.get("ip"))
            if ip is None:
                return result({"error": why})
            op = "register" if name == "pan_block_ip" else "unregister"
            # POST keeps the key out of the URL. httpx doesn't follow
            # redirects, so a 3xx (wrong hostname) reads as a failure.
            resp = httpx.post(
                api,
                data={
                    "type": "user-id",
                    "vsys": VSYS,
                    "key": api_key,
                    "cmd": _uid_message(op, ip),
                },
                verify=verify,
                timeout=30,
            )
            _, reason = parse(resp)
            where = f"tag {BLOCK_TAG} in {VSYS}"
            if reason and _no_op(resp, op):
                was = "already" if op == "register" else "not"
                msg = f"{ip} was {was} registered under {where}"
                return result({"success": True, "ip": ip, "message": msg})
            if reason:
                return result(
                    {"error": reason, "status_code": resp.status_code, "ip": ip}
                )
            return result(
                {
                    "success": True,
                    "ip": ip,
                    "message": f"{op}ed {ip} "
                    f"{'under' if op == 'register' else 'from'} {where}",
                }
            )

        elif name == "pan_get_threats":
            resp = httpx.get(
                api,
                params={
                    # `or 20`: see the aad_get_sign_ins note — a null limit would
                    # reach the wire as "nlogs=" under httpx.
                    "type": "log",
                    "log-type": "threat",
                    "key": api_key,
                    "nlogs": args.get("limit") or 20,
                },
                verify=verify,
                timeout=30,
            )
            # A bad key often comes back as HTTP 403, or 200 with status="error".
            # The body can echo the request, so the message stays the status only.
            body = resp.text[:400]
            refused = resp.status_code != 200 or 'status="error"' in body
            if refused:
                return result(
                    {
                        "error": f"Palo Alto answered HTTP {resp.status_code}",
                        "status_code": resp.status_code,
                    }
                )
            return result(
                {
                    "success": True,
                    "status_code": resp.status_code,
                    "message": "Read threat logs",
                }
            )

        return result({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return result({"error": str(e)})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "palo-alto",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="palo-alto",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
