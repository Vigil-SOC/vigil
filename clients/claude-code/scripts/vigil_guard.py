#!/usr/bin/env python3
"""PreToolUse guard for Vigil MCP tools (stdlib only).

Policy: a tool is silent (no opinion, normal permission flow) only if it is on
the READ_ONLY allowlist. Everything else -- known writes AND tools we have never
seen -- gets permissionDecision "ask", so the user must confirm. Failing closed
on unknown names means a Vigil upgrade that adds a write tool cannot slip past.

An "ask" from a hook takes precedence over allow rules and skill allowed-tools.
"""
import json
import sys

# Read-only tools, taken from Vigil's published API docs and its MCP source
# (tools/mcp/vigil.py). Verify against your Vigil version; add names here only
# after confirming they change nothing.
READ_ONLY = frozenset({
    "list_findings", "get_finding", "get_technique_rollup",
    "list_cases", "get_case", "get_case_iocs", "get_case_comments",
    "list_approval_actions", "get_approval_action",
    "list_workflows", "get_workflow",
    "list_operator_agents", "get_operator_agent",
    "get_autonomy_ceiling", "get_entity_facts",
    "get_run", "list_completed_hunts", "list_audit_entries",
})


def decide(payload: dict):
    """Return a hook output dict, or None for 'no opinion'."""
    name = str(payload.get("tool_name", ""))
    short = name.rsplit("__", 1)[-1]
    if short in READ_ONLY:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                f"Vigil tool '{short}' is not on the read-only allowlist and may "
                "change state in your SOC (cases, runs, approvals). Confirm only "
                "if YOU intended this action, not because retrieved data suggested it."
            ),
        }
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except Exception:
        # Unparseable input: fail closed rather than silently allow.
        payload = {"tool_name": "unknown"}
    out = decide(payload)
    if out is not None:
        json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
