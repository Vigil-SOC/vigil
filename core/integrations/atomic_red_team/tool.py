"""Atomic Red Team MCP server (stdio).

Thin invoke of one Atomic technique against a named ``environment_id``.
The operator installs the runner; this slice does not vendor the atomics
repo. The execute tool's JSON is the interface — an action trace, not
fabricated sensor events and not a ledger write.

Config comes from the descriptor: ``runner_path`` and ``atomics_path``
from the stored integration config under the ``atomic-red-team`` id.
"""

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
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from core.integrations._base.config import resolve
from core.integrations.atomic_red_team.descriptor import ATOMIC_RED_TEAM

logger = logging.getLogger(__name__)

RUNNER_TIMEOUT = 180

# argv the operator-installed runner must accept. Technique is required;
# ``--atomics-path`` is omitted when the field is empty (runner default).
_TECHNIQUE_FLAG = "--technique"
_ATOMICS_FLAG = "--atomics-path"


def result(data: Any) -> List[types.TextContent]:
    return [
        types.TextContent(type="text", text=json.dumps(data, indent=2, default=str))
    ]


def _load_config() -> Dict[str, str]:
    config = resolve(ATOMIC_RED_TEAM)
    return {
        "runner_path": (config.get("runner_path") or "").strip(),
        "atomics_path": (config.get("atomics_path") or "").strip(),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runner_argv(runner_path: str, technique: str, atomics_path: str) -> List[str]:
    argv = [runner_path, _TECHNIQUE_FLAG, technique]
    if atomics_path:
        argv.extend([_ATOMICS_FLAG, atomics_path])
    return argv


def execute_atomic(
    arguments: Optional[dict],
    config: Mapping[str, str],
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Dict[str, Any]:
    """Invoke one Atomic technique. Missing ``environment_id`` refuses first."""
    args = arguments or {}
    environment_id = str(args.get("environment_id") or "").strip()
    if not environment_id:
        return {"error": "environment_id required"}

    technique = str(args.get("technique") or "").strip()
    if not technique:
        return {"error": "technique required"}

    runner_path = (config.get("runner_path") or "").strip()
    if not runner_path:
        return {"error": "Atomic Red Team not configured (missing runner_path)"}

    argv = _runner_argv(runner_path, technique, config.get("atomics_path") or "")
    started_at = _now()
    try:
        completed = run(
            argv,
            capture_output=True,
            text=True,
            timeout=RUNNER_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return {"error": f"runner not found: {runner_path}"}
    except subprocess.TimeoutExpired as exc:
        return {
            "technique": technique,
            "environment_id": environment_id,
            "command": argv,
            "exit": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "started_at": started_at,
            "finished_at": _now(),
            "error": "runner timed out",
        }

    return {
        "technique": technique,
        "environment_id": environment_id,
        "command": argv,
        "exit": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
        "started_at": started_at,
        "finished_at": _now(),
    }


async def handle_list_tools() -> List[types.Tool]:
    return [
        types.Tool(
            name="atomic_red_team_execute",
            description=(
                "Execute one Atomic Red Team technique against a named "
                "environment_id (customer-provided range or staging replica). "
                "Returns an action trace (technique, command, exit, stdout/"
                "stderr, timestamps). Refuses if environment_id is missing. "
                "Does not capture or invent sensor telemetry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "technique": {
                        "type": "string",
                        "description": "ATT&CK technique id, e.g. T1003.001",
                    },
                    "environment_id": {
                        "type": "string",
                        "description": (
                            "Id of the sanctioned range or staging replica. "
                            "Required; the runner is not called without it."
                        ),
                    },
                },
                "required": ["technique", "environment_id"],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: Optional[dict]):
    if name != "atomic_red_team_execute":
        return result({"error": f"Unknown tool: {name}"})
    return result(execute_atomic(arguments, _load_config()))


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    try:
        content = await handle_call_tool(params.name, params.arguments)
    except Exception as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(exc))],
            is_error=True,
        )
    return types.CallToolResult(content=content)


server = Server(
    "atomic-red-team",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="atomic-red-team",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
