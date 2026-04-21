"""CAPE Sandbox MCP server (stdio).

CAPE (Config And Payload Extraction) is an open-source malware detonation
sandbox — the actively maintained fork of Cuckoo. This MCP server wraps the
CAPEv2 REST API so SOC agents can submit files/URLs for detonation and
retrieve behavioral reports, IOCs, and PCAPs.

Config comes from ``~/.deeptempo/integrations_config.json`` under the
``cape_sandbox`` key — fields: ``url``, ``api_key``.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

from core.config import get_integration_config

logger = logging.getLogger(__name__)
server = Server("cape-sandbox")


DEFAULT_TIMEOUT = 30
REPORT_TIMEOUT = 60


def result(data: Any) -> List[types.TextContent]:
    return [
        types.TextContent(type="text", text=json.dumps(data, indent=2, default=str))
    ]


def _load_config() -> Dict[str, str]:
    """Return CAPE config, falling back to env vars for container deployments."""
    config = get_integration_config("cape_sandbox") or {}
    url = config.get("url") or os.getenv("CAPE_SANDBOX_URL", "").rstrip("/")
    api_key = config.get("api_key") or os.getenv("CAPE_SANDBOX_API_KEY", "")
    return {"url": url.rstrip("/") if url else "", "api_key": api_key}


def _headers(api_key: str) -> Dict[str, str]:
    # CAPEv2 uses Token auth; some deployments use Bearer — Token is the canonical format.
    return {"Authorization": f"Token {api_key}"} if api_key else {}


def _extract_iocs(report: Dict[str, Any]) -> Dict[str, List[str]]:
    """Pull IPs, domains, hashes, mutexes, URLs out of a CAPE report."""
    iocs: Dict[str, set] = {
        "ips": set(),
        "domains": set(),
        "urls": set(),
        "hashes": set(),
        "mutexes": set(),
    }

    network = report.get("network") or {}
    for host in network.get("hosts", []) or []:
        if isinstance(host, dict):
            ip = host.get("ip")
            if ip:
                iocs["ips"].add(ip)
        elif isinstance(host, str):
            iocs["ips"].add(host)

    for dns in network.get("dns", []) or []:
        req = dns.get("request") if isinstance(dns, dict) else None
        if req:
            iocs["domains"].add(req)

    for http in network.get("http", []) or []:
        u = http.get("uri") if isinstance(http, dict) else None
        if u:
            iocs["urls"].add(u)

    for dropped in report.get("dropped", []) or []:
        if isinstance(dropped, dict):
            sha = dropped.get("sha256") or dropped.get("sha1") or dropped.get("md5")
            if sha:
                iocs["hashes"].add(sha)

    behavior = report.get("behavior") or {}
    for summary_key in ("mutexes", "mutex"):
        for mx in behavior.get("summary", {}).get(summary_key, []) or []:
            iocs["mutexes"].add(mx)

    return {k: sorted(v) for k, v in iocs.items()}


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    return [
        types.Tool(
            name="cape_submit_file",
            description=(
                "Submit a file to CAPE Sandbox for detonation. "
                "Accepts a local file_path or base64 file_b64 + filename."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to a local file",
                    },
                    "file_b64": {
                        "type": "string",
                        "description": "Base64-encoded file contents",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Filename when file_b64 is used",
                    },
                    "options": {
                        "type": "string",
                        "description": "CAPE options string (optional)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Analysis timeout in seconds",
                    },
                },
            },
        ),
        types.Tool(
            name="cape_submit_url",
            description="Submit a URL to CAPE Sandbox for behavioral analysis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["url"],
            },
        ),
        types.Tool(
            name="cape_get_report",
            description="Retrieve the full analysis report for a CAPE task.",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="cape_get_iocs",
            description="Extract IOCs (IPs, domains, URLs, dropped hashes, mutexes) from a completed CAPE task.",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="cape_get_pcap",
            description="Retrieve the PCAP download URL for a CAPE task (does not download bytes).",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="cape_list_tasks",
            description="List recent CAPE analysis tasks with status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max tasks (default 20)",
                    },
                    "offset": {"type": "integer", "description": "Pagination offset"},
                },
            },
        ),
        types.Tool(
            name="cape_search_hash",
            description=(
                "Look up prior CAPE analyses by file hash (md5/sha1/sha256). "
                "Use before submitting to avoid duplicate detonation."
            ),
            inputSchema={
                "type": "object",
                "properties": {"hash": {"type": "string"}},
                "required": ["hash"],
            },
        ),
        types.Tool(
            name="cape_task_status",
            description="Lightweight status check for a single task (pending / running / reported / failed).",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: Optional[dict]):
    cfg = _load_config()
    base = cfg["url"]
    api_key = cfg["api_key"]

    if not base:
        return result({"error": "CAPE Sandbox not configured (missing url)"})

    args = arguments or {}
    headers = _headers(api_key)

    try:
        if name == "cape_submit_file":
            file_path = args.get("file_path")
            file_b64 = args.get("file_b64")
            filename = args.get("filename")
            data = {}
            if args.get("options"):
                data["options"] = args["options"]
            if args.get("timeout"):
                data["timeout"] = str(args["timeout"])

            if file_path:
                with open(file_path, "rb") as fh:
                    files = {"file": (os.path.basename(file_path), fh)}
                    resp = requests.post(
                        f"{base}/apiv2/tasks/create/file/",
                        headers=headers,
                        files=files,
                        data=data,
                        timeout=DEFAULT_TIMEOUT,
                    )
            elif file_b64:
                if not filename:
                    return result(
                        {"error": "filename required when submitting via file_b64"}
                    )
                import base64

                files = {"file": (filename, base64.b64decode(file_b64))}
                resp = requests.post(
                    f"{base}/apiv2/tasks/create/file/",
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=DEFAULT_TIMEOUT,
                )
            else:
                return result(
                    {"error": "Provide either file_path or (file_b64 + filename)"}
                )

            resp.raise_for_status()
            return result(resp.json())

        if name == "cape_submit_url":
            target = args.get("url")
            if not target:
                return result({"error": "url required"})
            data = {"url": target}
            if args.get("timeout"):
                data["timeout"] = str(args["timeout"])
            resp = requests.post(
                f"{base}/apiv2/tasks/create/url/",
                headers=headers,
                data=data,
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            return result(resp.json())

        if name == "cape_get_report":
            tid = args.get("task_id")
            if not tid:
                return result({"error": "task_id required"})
            resp = requests.get(
                f"{base}/apiv2/tasks/get/report/{tid}/",
                headers=headers,
                timeout=REPORT_TIMEOUT,
            )
            resp.raise_for_status()
            return result(resp.json())

        if name == "cape_get_iocs":
            tid = args.get("task_id")
            if not tid:
                return result({"error": "task_id required"})
            resp = requests.get(
                f"{base}/apiv2/tasks/get/report/{tid}/",
                headers=headers,
                timeout=REPORT_TIMEOUT,
            )
            resp.raise_for_status()
            report = resp.json()
            # CAPE wraps the analysis under 'data' in some versions
            analysis = report.get("data") or report
            return result({"task_id": tid, "iocs": _extract_iocs(analysis)})

        if name == "cape_get_pcap":
            tid = args.get("task_id")
            if not tid:
                return result({"error": "task_id required"})
            return result(
                {
                    "task_id": tid,
                    "pcap_url": f"{base}/apiv2/tasks/get/pcap/{tid}/",
                    "note": "Authenticated download — use Authorization header.",
                }
            )

        if name == "cape_list_tasks":
            limit = int(args.get("limit", 20))
            offset = int(args.get("offset", 0))
            resp = requests.get(
                f"{base}/apiv2/tasks/list/{limit}/{offset}/",
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            return result(resp.json())

        if name == "cape_search_hash":
            h = args.get("hash")
            if not h:
                return result({"error": "hash required"})
            # CAPE search endpoint accepts md5/sha1/sha256 paths.
            # Probe sha256 first, then md5 as fallback.
            for hash_type in ("sha256", "md5"):
                resp = requests.get(
                    f"{base}/apiv2/tasks/search/{hash_type}/{h}/",
                    headers=headers,
                    timeout=DEFAULT_TIMEOUT,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tasks = data.get("data", []) if isinstance(data, dict) else data
                    if tasks:
                        return result(
                            {
                                "hash": h,
                                "hash_type": hash_type,
                                "found": True,
                                "tasks": tasks[:10],
                            }
                        )
            return result({"hash": h, "found": False, "tasks": []})

        if name == "cape_task_status":
            tid = args.get("task_id")
            if not tid:
                return result({"error": "task_id required"})
            resp = requests.get(
                f"{base}/apiv2/tasks/status/{tid}/",
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            return result(resp.json())

        return result({"error": f"Unknown tool: {name}"})

    except requests.HTTPError as e:
        return result(
            {
                "error": f"CAPE HTTP error: {e}",
                "status_code": getattr(e.response, "status_code", None),
            }
        )
    except Exception as e:
        logger.exception("CAPE tool call failed")
        return result({"error": str(e)})


async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="cape-sandbox",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
