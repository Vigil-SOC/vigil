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

REMOTE = "remote"

# Direct-action verbs that make an MCP tool destructive: calling it changes the
# world (isolates a host, blocks an IP, kills a process) and a later read cannot
# undo it. Chat reaches every other connected MCP tool on demand (see
# ``integration_tools``), but the chat surface has no approval-resume path — a
# parked call would hang forever, never gate — so these are out of its reach. Real containment
# goes through the approval queue (``create_approval_action``) and workflows, not
# ad-hoc chat calls.
_DESTRUCTIVE_VERBS = frozenset(
    {
        "isolate",
        "unisolate",
        "contain",
        "quarantine",
        "block",
        "unblock",
        "kill",
        "terminate",
        "shutdown",
        "disable",
        "deactivate",
        "suspend",
        "delete",
        "remove",
        "purge",
        "wipe",
        "revoke",
        "ban",
        "remediate",
        "detonate",
        "reset",
        "release",
    }
)
# A read-only lead verb (get_isolation_status, list_blocked_ips) is safe even
# when a destructive noun follows, so it overrides the verb check.
_READONLY_LEADS = frozenset(
    {
        "get",
        "list",
        "search",
        "describe",
        "fetch",
        "query",
        "show",
        "read",
        "lookup",
        "count",
        "stats",
        "status",
        "check",
    }
)


def _is_destructive_mcp(name: str) -> bool:
    """True for a server-prefixed MCP tool that performs an irreversible action.

    Every token of the id is read, the server prefix included. Vendor tools
    arrive as ``{server}_{tool}`` and Vigil's own arrive bare, so there is no
    one prefix to strip — and stripping the first token off a bare name takes
    the verb, which is the whole of what this decides on: ``isolate_host``
    would be read as ``host``.

    A read-only lead verb wins outright; otherwise any destructive verb token
    marks it. Reading the prefix too can only over-drop, and that is the side to
    err on — a spurious drop means chat recommends the action instead of calling
    it, whereas a missed one is an ungated detonation.

    ART execute is named, not verb-matched: adding ``execute`` to the verb set
    would also drop ``splunk_execute``.
    """
    if name in EXECUTE_IDS:
        return True
    tokens = name.split("_")
    if not tokens:
        return False
    if tokens[0] in _READONLY_LEADS:
        return False
    return any(tok in _DESTRUCTIVE_VERBS for tok in tokens)


# Weakest to strongest, so an agent is as hands-on as its most hands-on tool.
_CHANGES_RANK = ("read_only", "asks_first", "on_its_own")


def changes_for_tool(name: str) -> str:
    """What one tool does to the outside world.

    ``on_its_own`` for a destructive MCP tool with no approval gate; ``asks_first``
    for the approval tool and ART execute, which the gate covers; otherwise
    ``read_only``. Vigil's own case writes are not outside changes.
    """
    if name in EXECUTE_IDS or name == "create_approval_action":
        return "asks_first"
    return "on_its_own" if _is_destructive_mcp(name) else "read_only"


def changes_for_tools(tools: List[str]) -> str:
    """What an agent with these tools does: the strongest of its tools' answers."""
    return max(
        (changes_for_tool(t) for t in tools),
        key=_CHANGES_RANK.index,
        default="read_only",
    )


# A conversation is one answer at a time with a person waiting, so the ceiling is
# per turn rather than per run: they will say so long before a budget would.
DEFAULT_BUDGETS = {"max_calls": 12, "max_wall_ms": 300_000, "max_cost_usd": 2.0}
DEFAULT_RUNTIME = {"max_turns": 8, "result_cap": 20_000, "recall_limit": 3}

# A session id is the console's and is not a uuid; a run id is. Derived rather
# than generated so every turn of one conversation lands on the same run.
CONVERSATIONS = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")


def run_id_for(session_id: str) -> str:
    return str(uuid.uuid5(CONVERSATIONS, session_id))


# The MCP tools chat may reach through find/call_integration_tool: every
# connected one except direct-action tools (see ``_is_destructive_mcp``), which
# chat cannot safely gate, and those sharing a built-in's name — the backend
# answers those, and an agent's ``wanted`` list decides whether it may.
def integration_tools(
    mcp_tools: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    static = {t["name"] for t in ALL_TOOLS if t.get("name")}
    return [
        t
        for t in mcp_tools or []
        if t.get("name")
        and t["name"] not in static
        and not _is_destructive_mcp(t["name"])
    ]


def integrations_line(
    mcp_tools: Optional[List[Dict[str, Any]]], servers: Dict[str, str]
) -> str:
    """The system-prompt sentence naming what is connected, or "" for nothing."""
    names = sorted(
        {
            servers[t["name"]]
            for t in integration_tools(mcp_tools)
            if t["name"] in servers
        }
    )
    if not names:
        return ""
    return (
        f"Connected integrations: {', '.join(names)}. Their tools are not listed "
        f"here: call {FIND_INTEGRATION_TOOLS} to find one, then "
        f"{CALL_INTEGRATION_TOOL} to run it."
    )


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
    # ``approve_action`` is not dropped, and that is the decision rather than an
    # oversight: what it approves is an action a person already queued and can
    # already release from the approvals screen, so chat releasing it is the same
    # authority reached by a different door -- whereas ``isolate_host`` in chat
    # would be a detonation nobody queued. If that reading is ever revisited, the
    # thing to change is this list, not the verb set, which decides a different
    # question.
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
