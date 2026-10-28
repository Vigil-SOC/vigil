# The config layer a chat turn runs under. Assembled per request because which
# tools a conversation may reach depends on the agent the operator picked, and
# the agent registry lives on this side of the boundary.

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import yaml

from core.integrations.atomic_red_team.descriptor import EXECUTE_IDS
from core.llm.tool_schemas import (
    ALL_TOOLS,
    CALL_INTEGRATION_TOOL,
    FIND_INTEGRATION_TOOLS,
    INTEGRATION_TOOLS,
)
from core.response.ceiling import gated_tools

REMOTE = "remote"

# A conversation is one answer at a time with a person waiting, so the ceiling is
# per turn rather than per run: they will say so long before a budget would.
DEFAULT_BUDGETS = {"max_calls": 12, "max_wall_ms": 300_000, "max_cost_usd": 2.0}
DEFAULT_RUNTIME = {"max_turns": 8, "result_cap": 20_000, "recall_limit": 3}

# A session id is the console's and is not a uuid; a run id is. Derived rather
# than generated so every turn of one conversation lands on the same run.
CONVERSATIONS = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")


def run_id_for(session_id: str) -> str:
    return str(uuid.uuid5(CONVERSATIONS, session_id))


# The MCP tools chat may reach through find/call_integration_tool: Vigil's own,
# and outside tools whose stored class is read or read_unverified. Chat has no
# approval-resume path, so a parked call would hang rather than gate:
# reversible, irreversible and unclassified tools go through the approval queue
# and workflows instead. Those sharing a built-in's name are left out too — the
# backend answers those, and an agent's ``wanted`` list decides whether it may.
def integration_tools(
    mcp_tools: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    static = {t["name"] for t in ALL_TOOLS if t.get("name")}
    named = [t for t in mcp_tools or [] if t.get("name") and t["name"] not in static]
    gated = gated_tools(t["name"] for t in named)
    return [t for t in named if t["name"] not in gated]


def integrations_how(mcp_tools: Optional[List[Dict[str, Any]]]) -> str:
    """The system-prompt sentence on reaching integrations, or "" when none is.

    Under the same condition ``chat_config`` declares the two integration
    tools, so the system prompt and the tool list change together.
    """
    if not integration_tools(mcp_tools):
        return ""
    return (
        "Connected integrations' tools are not listed here: call "
        f"{FIND_INTEGRATION_TOOLS} to find one, then {CALL_INTEGRATION_TOOL} "
        "to run it."
    )


def integrations_line(
    mcp_tools: Optional[List[Dict[str, Any]]], servers: Dict[str, str]
) -> str:
    """The per-turn line naming what is connected, or "" for nothing."""
    names = sorted(
        {
            servers[t["name"]]
            for t in integration_tools(mcp_tools)
            if t["name"] in servers
        }
    )
    return f"Connected integrations: {', '.join(names)}." if names else ""


# A tool with no description is dropped rather than declared: the agent layer
# refuses one, because a model cannot choose between two blank tools.
def _declare(
    wanted: Optional[List[str]],
    mcp_tools: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    # A fixed core. The built-ins are curated, and a per-agent recommended-tools
    # list (``wanted``) narrows them. The connected integrations are not declared
    # one by one -- a default install's ran past 100 tools, which providers
    # refused and models chose among badly -- but found and called on demand
    # through two tools, declared whenever any is reachable, whatever ``wanted``
    # says: a user who connected an integration expects the assistant to use it.
    # Hunts curate separately (playbook_resolver).
    #
    # Chat has no approval-resume path, so ART execute is dropped by id.
    # ``approve_action`` stays declared and does not approve: it leaves the row
    # and points at Approvals (#2918). ``isolate_host`` in chat would be a
    # detonation nobody queued, which is a different question from this list.
    static = {t["name"]: t for t in ALL_TOOLS if t.get("name")}
    names = list(static) if wanted is None else [n for n in wanted if n in static]
    entries = [static[n] for n in names if n not in EXECUTE_IDS]
    if integration_tools(mcp_tools):
        entries += INTEGRATION_TOOLS
    declared = []
    for entry in entries:
        description = (entry.get("description") or "").strip()
        if not description:
            continue
        declared.append(
            {
                "id": entry["name"],
                "kind": REMOTE,
                "description": description,
                "parameters": entry.get("input_schema") or {"type": "object"},
            }
        )
    return declared


def granted_ids(wanted: Optional[List[str]]) -> List[str]:
    """Ids of the built-in tools a turn with this tool list will declare."""
    return [t["id"] for t in _declare(wanted)]


def chat_config(
    model: str,
    tools: Optional[List[str]] = None,
    mcp_tools: Optional[List[Dict[str, Any]]] = None,
    provider: Optional[str] = None,
    effort: Optional[str] = None,
    context_window: Optional[int] = None,
) -> str:
    """Render the agent layer's config document.

    ``provider`` is the gateway's provider name, carried separately from the
    model rather than folded into it: Bifrost routes ``<provider>/<model>`` but
    the price catalogue is keyed by the bare id, so the agent needs both. Left
    out when unknown, which is what every caller did before this existed.
    ``effort`` is the reasoning effort set on the model, left out when unset.
    ``context_window`` is the model's window in tokens, left out when unknown
    (0 or None); the agent layer only uses it to lower how much history it sends.
    """
    document = {
        "model": model,
        **({"provider": provider} if provider else {}),
        **({"effort": effort} if effort else {}),
        **(
            {"context_window": context_window}
            if context_window and context_window > 0
            else {}
        ),
        "budgets": DEFAULT_BUDGETS,
        "runtime": DEFAULT_RUNTIME,
        "tools": _declare(tools, mcp_tools),
        # A chat tool asks the person directly rather than parking on a
        # checkpoint: they are already in the conversation.
        "approvals": [],
        "thresholds": {},
    }
    return yaml.safe_dump(
        document, default_flow_style=False, sort_keys=False, allow_unicode=True
    )
