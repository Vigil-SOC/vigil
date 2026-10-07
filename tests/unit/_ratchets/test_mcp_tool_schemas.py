"""A published tool asks for the arguments it takes, and no others.

``**kwargs`` on a tool signature does not mean "and anything else" to the SDK:
it reads it as a parameter literally named ``kwargs`` and marks it **required**.
Every call then fails validation until the caller invents a value for an
argument the tool never uses -- and a caller reading the schema has no way to
know that is what is being asked of it.

This is the check the surface lacked: 31 tools shipped that way before anyone
called one from outside. It is a ratchet rather than a test of one tool,
because the mistake is a signature away on any tool anyone adds.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

# Python's own names for "whatever else was passed". None of them is a thing a
# caller can be asked for: they are an artifact of the signature, not arguments.
_VARARG_NAMES = {"kwargs", "kwds", "args", "varargs"}


def _published_tools():
    from tools.mcp.vigil import mcp

    return mcp._tool_manager.list_tools()


def test_no_tool_asks_for_an_argument_that_is_not_one():
    offenders = []
    for tool in _published_tools():
        schema = tool.parameters or {}
        named = set(schema.get("properties") or {}) | set(schema.get("required") or [])
        for leaked in sorted(named & _VARARG_NAMES):
            offenders.append(f"{tool.name}.{leaked}")

    assert not offenders, (
        "These tools publish a parameter that is really a Python catch-all, so "
        "a caller is asked for something the tool does not take -- and, when it "
        "lands in `required`, cannot call the tool at all without inventing a "
        f"value: {offenders}. Drop **kwargs from the signature and name the "
        "arguments the tool actually reads."
    )


def test_the_surface_publishes_tools_at_all():
    """Guards the guard: an empty list would pass the check above silently."""
    assert len(_published_tools()) > 0
