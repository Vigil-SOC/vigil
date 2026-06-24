"""Unit tests for services.chat.context_manager (GH #341).

The rolling-summary compression (``fold_overflow`` + summary prepending) is
central to the non-Anthropic / long-conversation behaviour introduced in the
Ollama PR, but shipped without coverage. These tests pin down the three
properties that matter:

  1. Role alternation after prepending the summary block — the Anthropic API
     rejects two consecutive assistant turns, which is exactly what the naive
     prepend produced when the sliding window started on an assistant turn.
  2. Deterministic, capped entity extraction in ``fold_overflow``.
  3. ``fold_overflow`` never grows the running summary without bound.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from services.chat.context_manager import (  # noqa: E402
    _SUMMARY_MAX_CHARS,
    ContextManager,
    _prepend_summary_block,
)

pytestmark = pytest.mark.unit


def _assert_alternates(messages):
    """No two adjacent messages may share a role (Anthropic requirement)."""
    for prev, cur in zip(messages, messages[1:]):
        assert prev["role"] != cur["role"], (
            f"consecutive {cur['role']} roles in {[m['role'] for m in messages]}"
        )


# ---------------------------------------------------------------------------
# _prepend_summary_block — role alternation
# ---------------------------------------------------------------------------


def test_prepend_no_summary_returns_window_unchanged():
    windowed = [{"role": "user", "content": "hi"}]
    out = _prepend_summary_block("", windowed)
    assert out == windowed
    # Must be a copy, not the same list object (callers mutate it).
    assert out is not windowed


def test_prepend_window_starting_with_user_adds_ack():
    windowed = [
        {"role": "user", "content": "what next?"},
        {"role": "assistant", "content": "investigate"},
    ]
    out = _prepend_summary_block("rolling summary", windowed)
    # summary(user) + ack(assistant) + window
    assert out[0]["role"] == "user"
    assert "rolling summary" in out[0]["content"]
    assert out[1]["role"] == "assistant"
    assert out[2:] == windowed
    _assert_alternates(out)


def test_prepend_window_starting_with_assistant_skips_ack():
    """Regression: a window sliced to begin on an assistant turn must not get
    the synthetic ack, or we'd emit user, assistant, assistant."""
    windowed = [
        {"role": "assistant", "content": "earlier analysis"},
        {"role": "user", "content": "and then?"},
    ]
    out = _prepend_summary_block("rolling summary", windowed)
    assert out[0]["role"] == "user"  # summary block
    assert out[1]["role"] == "assistant"  # the original window turn
    assert out[1]["content"] == "earlier analysis"
    assert len(out) == len(windowed) + 1  # only the summary was prepended
    _assert_alternates(out)


def test_prepend_empty_window_with_summary():
    out = _prepend_summary_block("summary", [])
    # No window to follow; summary + ack is still internally consistent.
    _assert_alternates(out)
    assert out[0]["role"] == "user"


# ---------------------------------------------------------------------------
# fold_overflow — deterministic, capped extraction
# ---------------------------------------------------------------------------


def test_fold_overflow_empty_returns_existing():
    assert ContextManager.fold_overflow([], "keep me") == "keep me"
    assert ContextManager.fold_overflow([]) == ""


def test_fold_overflow_extracts_entities():
    msgs = [
        {
            "role": "user",
            "content": (
                "Look at finding f-12345678-9abcdef0 and case-deadbeef99 "
                "from 10.0.0.5 with hash "
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa for CVE-2024-12345"
            ),
        },
        {"role": "assistant", "content": "This looks like lateral movement."},
    ]
    summary = ContextManager.fold_overflow(msgs)
    assert "f-12345678-9abcdef0" in summary
    assert "case-deadbeef99" in summary
    assert "10.0.0.5" in summary
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in summary
    assert "CVE-2024-12345" in summary
    assert "lateral movement" in summary  # assistant analysis snippet
    assert summary.startswith("[2 earlier messages]")


def test_fold_overflow_is_deterministic():
    msgs = [
        {"role": "user", "content": "ips 10.0.0.9 and 10.0.0.1 and 10.0.0.5"},
    ]
    a = ContextManager.fold_overflow(msgs)
    b = ContextManager.fold_overflow(msgs)
    assert a == b
    # IPs are sorted, so order is stable regardless of appearance order.
    assert a.index("10.0.0.1") < a.index("10.0.0.5") < a.index("10.0.0.9")


def test_fold_overflow_caps_entity_counts():
    # 30 distinct CVEs, but only 10 should survive the [:10] cap.
    cves = " ".join(f"CVE-2024-{1000 + i}" for i in range(30))
    summary = ContextManager.fold_overflow([{"role": "user", "content": cves}])
    found = [tok for tok in summary.split() if tok.rstrip(";,").startswith("CVE-")]
    assert len(found) == 10


def test_fold_overflow_bounded_growth():
    """Repeated folding of large messages must converge to a bounded size,
    not grow linearly with the number of folds."""
    big = {
        "role": "user",
        "content": "f-{:08x}-{:08x} ".format(1, 2) + ("attacker activity " * 2000),
    }
    summary = ""
    sizes = []
    for _ in range(400):
        summary = ContextManager.fold_overflow([big], summary)
        sizes.append(len(summary))
    # Bounded: the cap slices to _SUMMARY_MAX_CHARS, then a truncation marker is
    # prepended, so the size can nudge just past the cap but never further.
    assert all(s <= _SUMMARY_MAX_CHARS + 64 for s in sizes)
    # Saturates near the cap rather than climbing linearly with fold count.
    assert sizes[-1] >= _SUMMARY_MAX_CHARS - 200
    # Once saturated it plateaus (oscillates by at most one fold line), proving
    # it is bounded and not still accumulating.
    assert max(sizes[-10:]) - min(sizes[-10:]) <= 120


# ---------------------------------------------------------------------------
# prepare_context — end-to-end windowing + alternation
# ---------------------------------------------------------------------------


def test_prepare_context_windows_and_preserves_alternation(monkeypatch):
    import services.runtime_config as rc

    # Force a tiny window so the long conversation overflows.
    monkeypatch.setattr(
        rc, "get_ai_operations_setting", lambda key, default=None: 2
    )

    # 10 strictly-alternating turns starting with user.
    convo = []
    for i in range(10):
        role = "user" if i % 2 == 0 else "assistant"
        convo.append({"role": role, "content": f"turn {i}"})

    cm = ContextManager()
    prepared, overflow = cm.prepare_context(convo, summary="prior summary")

    assert overflow, "expected aged-out messages with a 2-turn window"
    assert prepared[0]["role"] == "user"  # summary block leads
    _assert_alternates(prepared)


def test_prepare_context_folds_overflow_into_summary_in_request(monkeypatch):
    """Aged-out messages must be folded into the prepended summary *this*
    request — not silently dropped — even when the incoming summary is empty.

    Regression for the PR #341 review: a per-request ClaudeService never
    hydrates its summary from disk, so ``summary`` is always "" here. The old
    code prepended only that empty summary, so the entities in aged-out turns
    vanished. They must now be folded synchronously and prepended.
    """
    import services.runtime_config as rc

    # window=2 -> keep the last 4 messages; everything older overflows.
    monkeypatch.setattr(rc, "get_ai_operations_setting", lambda key, default=None: 2)

    convo = [
        {"role": "user", "content": "investigate f-12345678-9abcdef0 please"},
        {"role": "assistant", "content": "tracing host 10.0.0.5"},
    ]
    for i in range(6):  # recent padding so the entity turns age out
        role = "user" if i % 2 == 0 else "assistant"
        convo.append({"role": role, "content": f"recent turn {i}"})

    cm = ContextManager()
    prepared, overflow = cm.prepare_context(convo, summary="")

    assert overflow, "expected aged-out messages with a 2-turn window"
    summary_block = prepared[0]["content"]
    assert "INVESTIGATION CONTEXT" in summary_block
    # SOC entities from the aged-out turns survive in the folded summary.
    assert "f-12345678-9abcdef0" in summary_block
    assert "10.0.0.5" in summary_block
