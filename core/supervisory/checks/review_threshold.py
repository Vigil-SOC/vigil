"""Check ``review_threshold`` against how people judged the findings it decided.

The processor offers a finding to intake when any of several things holds: a
high or critical severity, an isolate or block recommendation, a threat-intel
feed hit, a severity the install investigates, or a triage confidence at or
above ``review_threshold``. Only the last is this setting. So the check reads
the findings for which nothing else applied: for them, offered means the
confidence reached the line, and any other line would have offered exactly
those at or above it.

Over the last 30 days, among those findings a person judged (a noise mark or a
Case close, as the label counts read them), it replays each candidate line:

- missed: real attacks below the line, never offered;
- false alarms: findings above the line a person said were not real.

Missed attacks come first, then false alarms, as the scoring order says. A
difference of one missed attack is noise, so a line counts as better on missed
only with at least two fewer. With missed even, a line is suggested only when
it cuts false alarms clearly: by at least three and by a fifth. Nothing is
suggested below the floor of 20 judged findings.

Two things make the check lean toward not lowering the line. A real attack
below the line is judged only when someone found it another way, so missed
attacks are undercounted. And a dismissal that a threat-intel feed overrode is
left out, since whether the feed was on then is not recorded.

The value is set in the environment or INTENT.md, so the card cannot be applied
in one click; the settings list says where it comes from.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from core.config import get_settings
from core.detections.inventory.accurate import _closes, verdict_of
from core.response.config import ResponseConfig
from core.storage.intake_queue import intake_severity_band
from core.storage.models import Finding
from core.storage.unit_of_work import unit_of_work
from core.supervisory.feedback import (
    _NOT_REAL_DATA,
    FLOOR,
    WINDOW_DAYS,
    _in_person_closed_case,
    _naive,
)

KEY = "respond.review_threshold"
# Lines the replay tries, besides today's.
CANDIDATES = tuple(round(0.50 + 0.05 * i, 2) for i in range(10))
MISSED_MARGIN = 2
NOISE_CUT_MIN = 3
NOISE_CUT_SHARE = 0.2
TABLE_ROWS = 8

# Routes that offer a finding whatever its confidence.
_SEVERE = ("critical", "high")
_ACTING = ("isolate", "block")


def check() -> list[dict[str, Any]]:
    """At most one proposal: a better ``review_threshold``, or none."""
    now = _naive(datetime.now(UTC))
    judged = _judged(now - timedelta(days=WINDOW_DAYS), now)
    if len(judged) < FLOOR:
        return []
    current = float(ResponseConfig.from_settings().review_threshold)
    best = _best_line(judged, current)
    if best is None:
        return []
    return [_proposal(judged, current, best)]


def _judged(start: datetime, end: datetime) -> list[tuple[str, float, bool]]:
    """(finding id, confidence, real) for each finding only the line decided,
    first triaged in the window, whose winning determination is a person's."""
    severities = {s.lower() for s in get_settings().daemon_investigate_severities}
    where = (
        Finding.first_verdict_at >= start,
        Finding.first_verdict_at < end,
        Finding.data_source.notin_(_NOT_REAL_DATA),
        or_(Finding.noise_marked_at.isnot(None), _in_person_closed_case()),
    )
    out = []
    with unit_of_work() as session:
        closes = _closes(session, *where, sources=None)
        rows = session.execute(
            select(
                Finding.finding_id,
                Finding.severity,
                # Only the keys the offer rule reads, not the whole triage record.
                Finding.ai_enrichment["recommended_action"].astext,
                Finding.ai_enrichment["triage_confidence"],
                Finding.ai_enrichment["enrichment"]["threat_indicators"],
                Finding.noise_marked_at,
                Finding.noise_marked_by_kind,
            ).where(*where)
        )
        for fid, severity, action, confidence, indicators, marked, kind in rows:
            enrichment = {
                "recommended_action": action,
                "triage_confidence": confidence,
                "enrichment": {"threat_indicators": indicators},
            }
            confidence = _line_decided(severity, enrichment, severities)
            if confidence is None:
                continue
            won = verdict_of(closes.get(fid, []), marked, kind)
            if won is None or not won[1]:
                continue
            out.append((fid, confidence, bool(won[0])))
    return out


def _line_decided(
    severity: str | None, enrichment: dict[str, Any], investigated: set[str]
) -> float | None:
    """The finding's triage confidence when only ``review_threshold`` could have
    offered it, else None. Mirrors the processor's offer rule."""
    action = str(enrichment.get("recommended_action") or "").lower()
    if action == "dismiss" or action in _ACTING:
        return None
    # As the processor reads it: the raw severity for the high and critical
    # route, the intake band (anything unknown is "unknown") for the severities
    # the install investigates.
    if str(severity or "").lower() in _SEVERE:
        return None
    if intake_severity_band("detection", finding_severity=severity) in investigated:
        return None
    if (enrichment.get("enrichment") or {}).get("threat_indicators"):
        return None
    confidence = enrichment.get("triage_confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    return float(confidence)


def _score(judged: list[tuple[str, float, bool]], line: float) -> tuple[int, int, int]:
    """(missed real attacks, false alarms offered, findings offered) at ``line``."""
    missed = sum(1 for _, c, real in judged if real and c < line)
    noise = sum(1 for _, c, real in judged if not real and c >= line)
    offered = sum(1 for _, c, _ in judged if c >= line)
    return missed, noise, offered


def _best_line(judged: list[tuple[str, float, bool]], current: float) -> float | None:
    """The candidate line that clearly beats ``current``, or None."""
    missed0, noise0, _ = _score(judged, current)
    better, even = [], []
    for line in CANDIDATES:
        if abs(line - current) < 1e-9:
            continue
        missed, noise, _ = _score(judged, line)
        if missed <= missed0 - MISSED_MARGIN:
            better.append((missed, noise, abs(line - current), line))
        elif abs(missed - missed0) < MISSED_MARGIN and missed <= missed0:
            cut = noise0 - noise
            if cut >= NOISE_CUT_MIN and cut >= NOISE_CUT_SHARE * max(noise0, 1):
                even.append((noise, abs(line - current), line))
    if better:
        return min(better)[3]
    if even:
        return min(even)[2]
    return None


def _proposal(
    judged: list[tuple[str, float, bool]], current: float, line: float
) -> dict[str, Any]:
    missed0, noise0, offered0 = _score(judged, current)
    missed1, noise1, offered1 = _score(judged, line)
    lower = line < current
    low, high = sorted((current, line))
    band = sorted(
        (j for j in judged if low <= j[1] < high), key=lambda j: j[1], reverse=True
    )
    real_in_band = sum(1 for _, _, real in band if real)
    if lower:
        why = (
            f"{real_in_band} of {len(band)} findings that scored {low:.2f}–{high:.2f} "
            "were later judged real attacks, and were never offered for investigation"
        )
        plain = (
            f"Lowering the line to {line:.2f} would have sent "
            f"{missed0 - missed1} more real attacks to investigation in the last "
            f"{WINDOW_DAYS} days, at the cost of {noise1 - noise0} more false alarms."
        )
        verb = "Lower"
    else:
        why = (
            f"{len(band) - real_in_band} of {len(band)} findings offered only because "
            f"they scored {low:.2f}–{high:.2f} were judged not real"
        )
        plain = (
            f"Raising the line to {line:.2f} would have kept {noise0 - noise1} false "
            f"alarms out of investigation in the last {WINDOW_DAYS} days, without "
            "missing another real attack."
        )
        verb = "Raise"
    replay = _replay_line(missed0 - missed1, noise1 - noise0)
    return {
        "fn": "triage",
        "key": KEY,
        "verb": verb,
        "from_value": f"{current:g}",
        "to_value": f"{line:.2f}",
        "widens_autonomy": False,
        "card": {
            "why": why,
            "evidence": f"{len(judged)} judged findings · {WINDOW_DAYS} days",
            "replay": replay,
            "check": None,
            "plain_words": plain,
            "table": {
                "title": f"Findings that scored {low:.2f}–{high:.2f}",
                "columns": [
                    {"label": "Finding", "kind": "mono"},
                    {"label": "Confidence", "kind": "mono"},
                    {"label": "Judged", "kind": "text"},
                ],
                "rows": [
                    [
                        {"text": fid},
                        {"text": f"{c:.2f}"},
                        {"text": "real attack" if real else "not real", "attack": real},
                    ]
                    for fid, c, real in band[:TABLE_ROWS]
                ],
                "foot": (
                    f"{len(band) - TABLE_ROWS} more · all from the last {WINDOW_DAYS} days"
                    if len(band) > TABLE_ROWS
                    else f"All from the last {WINDOW_DAYS} days"
                ),
            },
            "replay_title": f"Last {WINDOW_DAYS} days at {current:g} and at {line:.2f}",
            "replay_rows": [
                _row(
                    "Real attacks never offered", missed0, missed1, lower_is_better=True
                ),
                _row("False alarms offered", noise0, noise1, lower_is_better=True),
                _row("Findings offered", offered0, offered1, lower_is_better=None),
            ],
        },
    }


def _replay_line(fewer_missed: int, more_noise: int) -> str:
    parts = []
    if fewer_missed > 0:
        parts.append(f"{fewer_missed} more real attacks offered")
    elif fewer_missed < 0:
        parts.append(f"{-fewer_missed} more real attacks missed")
    if more_noise > 0:
        parts.append(f"{more_noise} more false alarms")
    elif more_noise < 0:
        parts.append(f"{-more_noise} fewer false alarms")
    return ", ".join(parts) or "no change"


def _row(
    label: str, now: int, then: int, lower_is_better: bool | None
) -> dict[str, str]:
    if lower_is_better is None or now == then:
        effect = "same"
    elif (then < now) == lower_is_better:
        effect = "better"
    else:
        effect = "worse"
    return {"label": label, "now": str(now), "next": str(then), "effect": effect}
