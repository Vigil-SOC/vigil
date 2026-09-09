# The config layer a chat turn runs under. Assembled per request because which
# tools a conversation may reach depends on the agent the operator picked, and
# the agent registry lives on this side of the boundary.

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import yaml

from core.llm.tool_schemas import ALL_TOOLS

REMOTE = "remote"

# Direct-action verbs that make an MCP tool destructive: calling it changes the
# world (isolates a host, blocks an IP, kills a process) and a later read cannot
# undo it. Chat declares connected MCP tools unconditionally (see ``_declare``),
# but the chat surface has no approval-resume path — a parked call would hang
# forever, never gate — so these are dropped from chat entirely. Real containment
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


# MemPalace, the prose store #735 moves the agents off — not Episodic Memory,
# which chat reaches through the static recall_entity tool and this does not
# touch. The reason is not the one above: a palace write is gateable, it simply
# must not happen. Chat is the surface where an analyst thinks aloud, and prose
# written there and read back later as history is the promotion of guess to fact
# ADR 0015 forbids.
#
# Whole-server rather than by verb, which also drops chat's palace reads. The
# palace ships 44 tools and the write half is the larger, so a read allow-list
# goes stale toward more writes — the direction that matters. #735's factory
# scope says drop `mempalace_*`; narrowing this to write verbs is a one-line
# change if the reads turn out to be missed. The server stays connected: the
# daemon still uses it, and retiring it is #730's work.
_MEMORY_PALACE_PREFIX = "mempalace_"


def _is_destructive_mcp(name: str) -> bool:
    """True for a server-prefixed MCP tool that performs an irreversible action.

    MCP names arrive as ``{server}_{tool}``; the action is the tool part. A
    read-only lead verb wins outright; otherwise any destructive verb token marks
    it. Deliberately conservative — a spurious drop just means chat recommends the
    action instead of calling it, whereas a missed one is an ungated detonation.
    """
    action = name.split("_", 1)[1] if "_" in name else name
    tokens = action.split("_")
    if not tokens:
        return False
    if tokens[0] in _READONLY_LEADS:
        return False
    return any(tok in _DESTRUCTIVE_VERBS for tok in tokens)


# A conversation is one answer at a time with a person waiting, so the ceiling is
# per turn rather than per run: they will say so long before a budget would.
DEFAULT_BUDGETS = {"max_calls": 12, "max_wall_ms": 300_000, "max_cost_usd": 2.0}
DEFAULT_RUNTIME = {"max_turns": 8, "result_cap": 20_000, "recall_limit": 3}

# A session id is the console's and is not a uuid; a run id is. Derived rather
# than generated so every turn of one conversation lands on the same run.
CONVERSATIONS = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")


def run_id_for(session_id: str) -> str:
    return str(uuid.uuid5(CONVERSATIONS, session_id))


# A tool with no description is dropped rather than declared: the agent layer
# refuses one, because a model cannot choose between two blank tools.
def _declare(
    wanted: Optional[List[str]],
    mcp_tools: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    # Two surfaces. Static built-ins are curated: a per-agent recommended-tools
    # list (``wanted``) narrows them. Connected MCP integrations (server-prefixed,
    # e.g. virustotal_get_ip_report) are ALWAYS offered when present — a user who
    # connected an integration expects the assistant to use it regardless of any
    # agent's tool list, and hunts curate separately (playbook_resolver). So a
    # ``wanted`` list filters only the built-ins; live integrations are appended —
    # except two kinds: direct-action MCP tools (see ``_is_destructive_mcp``),
    # which chat cannot safely gate, and the memory palace (see above), which
    # chat must not write.
    static = {t["name"]: t for t in ALL_TOOLS if t.get("name")}
    mcp = {t["name"]: t for t in (mcp_tools or []) if t.get("name")}
    static_names = (
        list(static) if wanted is None else [n for n in wanted if n in static]
    )
    mcp_names = [
        n
        for n in mcp
        if n not in static_names
        and not _is_destructive_mcp(n)
        and not n.startswith(_MEMORY_PALACE_PREFIX)
    ]
    names = static_names + mcp_names
    catalogue = {**static, **mcp}
    declared = []
    for name in names:
        entry = catalogue[name]
        description = (entry.get("description") or "").strip()
        if not description:
            continue
        declared.append(
            {
                "id": name,
                "kind": REMOTE,
                "description": description,
                "parameters": entry.get("input_schema") or {"type": "object"},
            }
        )
    return declared


def chat_config(
    model: str,
    tools: Optional[List[str]] = None,
    mcp_tools: Optional[List[Dict[str, Any]]] = None,
    provider: Optional[str] = None,
) -> str:
    """Render the agent layer's config document.

    ``provider`` is the gateway's provider name, carried separately from the
    model rather than folded into it: Bifrost routes ``<provider>/<model>`` but
    the price catalogue is keyed by the bare id, so the agent needs both. Left
    out when unknown, which is what every caller did before this existed.
    """
    document = {
        "model": model,
        **({"provider": provider} if provider else {}),
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
