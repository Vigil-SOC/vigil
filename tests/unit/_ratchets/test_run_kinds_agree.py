"""The run-kind vocabulary is declared twice and must say the same thing.

Python enqueues a job naming a run kind; TypeScript validates it against a closed
union and resolves it to an arch. Nothing checked the two lists agreed, and they
had already drifted -- TypeScript carried ``tally`` and Python did not, so a
conformance run could be enqueued by hand and refused, or a kind added on one
side reached production missing from the other.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PYTHON = REPO_ROOT / "core" / "agents" / "queue.py"
TYPESCRIPT = REPO_ROOT / "services" / "agent" / "contracts" / "events.ts"
REGISTRY = REPO_ROOT / "services" / "agent" / "plugins" / "kinds.ts"

# The conformance workflow. It proves the harness boundary holds without a real
# domain, so it is deliberately not something the backend can enqueue. subagent is
# a child run -- a judge a run started on a ledger of its own -- which only the
# agent layer starts, never a queue, and which resolves to no arch.
BACKEND_EXCLUDES = frozenset({"tally", "subagent"})

pytestmark = pytest.mark.unit


def python_kinds() -> tuple[str, ...]:
    for node in ast.walk(ast.parse(PYTHON.read_text())):
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "RUN_KINDS" for target in node.targets
        ):
            return tuple(element.value for element in node.value.elts)
    raise AssertionError(f"RUN_KINDS not found in {PYTHON}")


def typescript_kinds() -> tuple[str, ...]:
    match = re.search(r"RUN_KINDS\s*=\s*\[([^\]]*)\]", TYPESCRIPT.read_text())
    assert match, f"RUN_KINDS not found in {TYPESCRIPT}"
    return tuple(re.findall(r'"([^"]+)"', match.group(1)))


def registered_kinds() -> set[str]:
    # Keys of the REGISTERED record: the kinds that resolve to an arch file.
    body = REGISTRY.read_text()
    start = body.index("const REGISTERED")
    return set(re.findall(r"^  ([a-z_]+):", body[start:], re.MULTILINE))


def test_every_kind_the_backend_enqueues_is_one_typescript_accepts():
    stray = sorted(set(python_kinds()) - set(typescript_kinds()))
    assert not stray, (
        "core/agents/queue.py can enqueue kinds the agent layer will refuse:\n  "
        + "\n  ".join(stray)
    )


def test_every_typescript_kind_is_enqueueable_or_deliberately_not():
    missing = sorted(set(typescript_kinds()) - set(python_kinds()) - BACKEND_EXCLUDES)
    assert not missing, (
        "the agent layer accepts kinds the backend cannot enqueue. Add them to "
        "core/agents/queue.py, or to BACKEND_EXCLUDES here with the reason:\n  "
        + "\n  ".join(missing)
    )


def test_every_kind_resolves_to_an_arch():
    # A kind in the union with no registry entry fails at startup rather than
    # seven iterations in -- but only once something tries to run it.
    orphans = sorted(set(typescript_kinds()) - registered_kinds() - BACKEND_EXCLUDES)
    assert (
        not orphans
    ), "these run kinds are accepted but resolve to no arch:\n  " + "\n  ".join(orphans)


def test_the_exclusion_list_stays_honest():
    stale = sorted(kind for kind in BACKEND_EXCLUDES if kind not in typescript_kinds())
    assert (
        not stale
    ), "BACKEND_EXCLUDES names kinds that no longer exist:\n  " + "\n  ".join(stale)
