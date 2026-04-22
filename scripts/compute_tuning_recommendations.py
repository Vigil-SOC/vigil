"""Data-driven tuning recommendations from LLMInteractionLog (GH #84 PR-E).

PR-D shipped with best-guess defaults for four cost/perf toggles:

  * ``CLAUDE_HISTORY_WINDOW`` (20 turns)
  * ``TOOL_RESPONSE_BUDGET_DEFAULT`` (8000 tokens)
  * ``CLAUDE_THINKING_BUDGET`` (10000 tokens, daemon-wide)
  * per-agent ``thinking_budget`` values in ``services/soc_agents.py``

PR-E's completion criterion was "re-tune defaults using two weeks of
post-merge data." This script is the tooling side of that: point it at
the LLMInteractionLog table and it'll spit out evidence-based
recommendations (p50 / p95 / max of the relevant distributions).

Operators run this periodically — say, monthly — and apply the
recommendations through Settings → AI Config → AI Operations (PR-F) or
by editing ``services/soc_agents.py`` for per-agent thinking budgets.

Usage::

    DATABASE_URL=postgresql://... python scripts/compute_tuning_recommendations.py
    DATABASE_URL=...  python scripts/compute_tuning_recommendations.py --days 30

Exit code 0 always — this is informational, never a gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Dict, Iterable, List, Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))


def _p95(values: List[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = max(0, int(0.95 * (len(ordered) - 1)))
    return ordered[idx]


def _fetch_rows(days: int) -> List[Dict]:
    """Pull the last ``days`` of LLMInteractionLog rows. Returns simple
    dicts so the caller doesn't need SQLAlchemy loaded."""
    from database.connection import get_session
    from database.models import LLMInteractionLog

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    with get_session() as session:
        rows = (
            session.query(LLMInteractionLog)
            .filter(LLMInteractionLog.created_at >= cutoff)
            .all()
        )
        return [
            {
                "agent_id": r.agent_id,
                "input_tokens": int(r.input_tokens or 0),
                "output_tokens": int(r.output_tokens or 0),
                "thinking_enabled": bool(r.thinking_enabled),
                "thinking_budget": int(r.thinking_budget) if r.thinking_budget else None,
                "tool_results": r.tool_results or [],
                "thinking_content": r.thinking_content or "",
                "request_messages": r.request_messages or [],
                "cost_usd": float(r.cost_usd) if r.cost_usd else 0.0,
            }
            for r in rows
        ]


def recommend_thinking_budgets(rows: Iterable[Dict]) -> Dict[str, Dict[str, int]]:
    """Per-agent thinking-budget recommendations.

    Reads ``thinking_content`` length to estimate actual reasoning-token
    use, then recommends the p95 rounded up to the next 500. Leaves 5%
    headroom for unusual prompts while cutting the over-provisioning most
    agents currently ship with.

    ``thinking_content`` is a str, not a token count — we estimate
    tokens as ``len(text) // 4`` which matches ``_estimate_tokens`` in
    ClaudeService.
    """
    by_agent: Dict[str, List[int]] = {}
    for row in rows:
        if not row["thinking_enabled"]:
            continue
        used = len(row["thinking_content"]) // 4
        if used == 0:
            continue
        agent_id = row["agent_id"] or "unknown"
        by_agent.setdefault(agent_id, []).append(used)

    out: Dict[str, Dict[str, int]] = {}
    for agent_id, samples in sorted(by_agent.items()):
        p50 = int(median(samples))
        p95 = _p95(samples)
        # Round up to the nearest 500 + 5% headroom; clamp floor at 1000.
        recommended = max(1000, int(round(p95 * 1.05 / 500.0)) * 500)
        out[agent_id] = {
            "samples": len(samples),
            "p50": p50,
            "p95": p95,
            "max": max(samples),
            "recommended_thinking_budget": recommended,
        }
    return out


def recommend_history_window(rows: Iterable[Dict]) -> Dict[str, int]:
    """How long are conversation histories in practice?

    We don't have a direct "turn count" per row — ``request_messages`` is
    the already-windowed list the model saw. Reporting its p50/p95 tells
    us whether the current window (default 20 turns = 40 messages) is
    too tight or too loose.
    """
    lengths: List[int] = [len(row["request_messages"]) for row in rows]
    if not lengths:
        return {"samples": 0}
    return {
        "samples": len(lengths),
        "p50_messages": int(median(lengths)),
        "p95_messages": _p95(lengths),
        "max_messages": max(lengths),
        "current_default_messages": 40,  # CLAUDE_HISTORY_WINDOW=20 turns
        "notes": (
            "If p95 < current_default, the window is loose — tightening saves "
            "nothing but is safe. If p95 >= current_default, the window is "
            "clipping real conversations and summarization is likely firing "
            "more than expected; consider raising."
        ),
    }


def recommend_tool_response_budget(rows: Iterable[Dict]) -> Dict[str, object]:
    """Recommend the default tool-response truncation budget.

    Looks at tool_result text lengths across the window. A too-tight
    budget costs analysis quality (truncation markers appear mid-evidence);
    a too-loose one burns input tokens. Per-tool overrides in
    ``ClaudeService.TOOL_RESPONSE_BUDGETS`` handle the outliers.
    """
    sizes: List[int] = []
    for row in rows:
        for tr in row["tool_results"]:
            text = ""
            if isinstance(tr, dict):
                content = tr.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text += block.get("text", "")
                elif isinstance(content, str):
                    text = content
            if text:
                sizes.append(len(text) // 4)
    if not sizes:
        return {"samples": 0}
    return {
        "samples": len(sizes),
        "p50_tokens": int(median(sizes)),
        "p95_tokens": _p95(sizes),
        "max_tokens": max(sizes),
        "current_default": 8000,
        "recommended_default": max(2000, int(round(_p95(sizes) / 1000.0)) * 1000),
        "notes": (
            "Recommendation = p95 rounded to nearest 1k. Per-tool overrides "
            "for legitimately-large tools (get_raw_logs, splunk_search, etc.) "
            "are in ClaudeService.TOOL_RESPONSE_BUDGETS — tune those "
            "separately by examining the top offenders for tool_name."
        ),
    }


def recommend_daemon_thinking_budget(rows: Iterable[Dict]) -> Dict[str, int]:
    """Recommend ``CLAUDE_THINKING_BUDGET`` (the daemon's process-wide default).

    Covers the "no agent_id" bucket — everything the autonomous daemon
    runs outside the named sub-agent profiles.
    """
    samples: List[int] = []
    for row in rows:
        if not row["thinking_enabled"]:
            continue
        if row["agent_id"]:  # per-agent rows are already analyzed
            continue
        used = len(row["thinking_content"]) // 4
        if used:
            samples.append(used)
    if not samples:
        return {"samples": 0}
    return {
        "samples": len(samples),
        "p50": int(median(samples)),
        "p95": _p95(samples),
        "max": max(samples),
        "current_default": 10000,
        "recommended": max(2000, int(round(_p95(samples) * 1.05 / 500.0)) * 500),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="How many days of history to analyze (default 14)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format",
    )
    args = parser.parse_args()

    try:
        rows = _fetch_rows(args.days)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Unable to read LLMInteractionLog: {exc}", file=sys.stderr)
        print(
            "  Check DATABASE_URL. This script is read-only — it needs the",
            "  same DB the backend + daemon write interaction logs to.",
            sep="\n",
            file=sys.stderr,
        )
        return 1

    report: Dict[str, object] = {
        "window_days": args.days,
        "total_rows": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "per_agent_thinking_budget": recommend_thinking_budgets(rows),
        "history_window": recommend_history_window(rows),
        "tool_response_budget_default": recommend_tool_response_budget(rows),
        "daemon_thinking_budget": recommend_daemon_thinking_budget(rows),
    }

    if args.format == "json":
        print(json.dumps(report, indent=2))
        return 0

    # Text report
    print(f"Analyzed {len(rows)} LLM interactions over the last {args.days} day(s).")
    print()

    print("## Per-agent thinking_budget recommendations")
    pa = report["per_agent_thinking_budget"]
    if not pa:
        print("  (no thinking-enabled agent rows)")
    else:
        print(f"  {'agent':<22}{'samples':>9}{'p50':>8}{'p95':>8}{'max':>8}  {'recommended':>12}")
        for agent_id, stats in pa.items():  # type: ignore[assignment]
            print(
                f"  {agent_id:<22}"
                f"{stats['samples']:>9}"
                f"{stats['p50']:>8}"
                f"{stats['p95']:>8}"
                f"{stats['max']:>8}"
                f"  {stats['recommended_thinking_budget']:>12}"
            )
        print("\n  Apply via services/soc_agents.py → agent config → thinking_budget field.")

    def _print_block(title: str, payload: Dict):
        print(f"\n## {title}")
        if payload.get("samples", 0) == 0:
            print("  (no samples)")
            return
        for k, v in payload.items():
            print(f"  {k}: {v}")

    _print_block("history_window (CLAUDE_HISTORY_WINDOW)", report["history_window"])  # type: ignore[arg-type]
    _print_block(
        "tool_response_budget_default (TOOL_RESPONSE_BUDGET_DEFAULT)",
        report["tool_response_budget_default"],  # type: ignore[arg-type]
    )
    _print_block(
        "daemon_thinking_budget (CLAUDE_THINKING_BUDGET)",
        report["daemon_thinking_budget"],  # type: ignore[arg-type]
    )

    print()
    print("Apply non-agent recommendations via Settings → AI Config → AI Operations.")
    print("Per-agent thinking_budget edits land in services/soc_agents.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
