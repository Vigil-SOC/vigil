"""Data-driven per-agent thinking-budget recommendations (GH #84 PR-E).

PR-D shipped best-guess ``thinking_budget`` values on each agent in
``core/agents/builtins.py``. Point this script at LLMInteractionLog and
it prints evidence-based recommendations (p50 / p95 / max).

Operators run it periodically and apply the numbers by editing
``core/agents/builtins.py``.

Connects through ``get_session()`` (encrypted DSN / POSTGRES_*), the same
path the backend and daemon use. ``DATABASE_URL`` is not consulted.

Usage::

    python scripts/compute_tuning_recommendations.py
    python scripts/compute_tuning_recommendations.py --days 30

Exit code 0 always — this is informational, never a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Dict, Iterable, List

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


def _p95(values: List[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = max(0, int(0.95 * (len(ordered) - 1)))
    return ordered[idx]


def _fetch_rows(days: int) -> List[Dict]:
    """Pull the last ``days`` of LLMInteractionLog rows. Returns simple
    dicts so the caller doesn't need SQLAlchemy loaded."""
    from core.storage.connection import get_session
    from core.storage.models import LLMInteractionLog

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
                "thinking_enabled": bool(r.thinking_enabled),
                "thinking_content": r.thinking_content or "",
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
            "  Check POSTGRES_* / the encrypted POSTGRESQL_CONNECTION_STRING.",
            "  This script is read-only — it uses get_session() and needs the",
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
        print(
            f"  {'agent':<22}{'samples':>9}{'p50':>8}{'p95':>8}{'max':>8}  {'recommended':>12}"
        )
        for agent_id, stats in pa.items():  # type: ignore[assignment]
            print(
                f"  {agent_id:<22}"
                f"{stats['samples']:>9}"
                f"{stats['p50']:>8}"
                f"{stats['p95']:>8}"
                f"{stats['max']:>8}"
                f"  {stats['recommended_thinking_budget']:>12}"
            )
        print(
            "\n  Apply via core/agents/builtins.py → agent config → thinking_budget field."
        )

    print()
    print("Per-agent thinking_budget edits land in core/agents/builtins.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
