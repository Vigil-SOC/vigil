"""Caught: whether a rule version alerted on a real attack step.

The evidence is Vigil's own: compose runs scored as ``learning_episodes`` scores
them (each emulated step's cited findings, judged by
``reconstruction.coverage_report``'s step rule), and proven hunt and Case
Verdicts over the last LOOKBACK_DAYS, each one step reconstructed as
``gaps.missed_runs_report`` does (a cut-off Findings read gives no credit).
LogLM and probe citations are not a rule alerting and are skipped.

Each cited finding resolves to the version that fired it through
``catalog.resolve_findings``, which picks the version first seen at or before
the alert, so a version is never credited on alerts from before it existed.
It passes only when its rule declares the step's technique, at parent level
(T1059.001 ~ T1059): correlation is host, IP or user plus time, so without it
any noisy rule on the host would pass. A pass records ``technique`` and ``at``,
and from an emulation also ``hostname`` and ``run_id``.

Emulation runs finished in the last RUNS_WINDOW are read on every sync, so a
catch is recorded within the hour. Verdicts are read at most once per
HOLD_SECONDS per process. An emulated step that missed (the runner exited 0, past
``reconstruction.LAG``, and no rule-origin Finding cites it) writes ``fail`` on a
version whose stored pass came from an emulation of the same technique (parent
level) on the same host, and is newer than that pass. The change row keeps the
earlier pass, so the pass -> fail shows as a flag; a later catch writes pass
again. A version with no result is untested.

A rule in shadow has no Findings to cite, so each scored emulated step (not
unknown, with a host and a window) on a technique an active overlay copy in
shadow declares (parent level) searches that copy once in its SIEM, as rollout
runs it (``rollout._query`` / ``_splunk_query`` / ``_wazuh_query``), over the
step's window padded by ``siem_replay.CLAIM_PAD``, spending one search of that
SIEM's hourly budget under ``caught:<rule id>``. A hit on the step's host inside
that window (``siem_replay.Claim.holds``) is a pass on the copy's latest version,
which ``rollout.shown_to_fire`` counts. A spent budget, missing config or failed
search writes nothing: the step is tried again on a later sync while its run is
in RUNS_WINDOW; a search that answered is not repeated for that step. This
path never writes fail, and skips a copy already passing. Its pass carries
``shadow``: while the copy is still in shadow, a later step no live rule caught
does not fail it (that miss is about live rules, not this copy).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import or_, select, tuple_

from core.detections import gaps
from core.detections.inventory import catalog, readers, sees
from core.detections.reconstruction import _LOG_LM
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion, Finding

# The sources Caught credits or fails: a provider inventory (Sentinel) is not one.
_JUDGED = DetectionRule.source.in_(catalog.RESOLVES) | (
    DetectionRule.source == readers.OVERLAY_SOURCE
)

logger = logging.getLogger(__name__)

CHECK = "caught"
LOOKBACK_DAYS = 30
HOLD_SECONDS = 24 * 3600.0
_SKIPPED = (_LOG_LM, "probe")

RUNS_WINDOW = timedelta(days=1)

# When this process last read Verdicts (time.monotonic()).
_last_swept: Optional[float] = None
# (rule id, version, run id, step start) a shadow search answered, with the run's
# time: the step is past LAG, so the answer stands and is not searched again.
_answered: Dict[Tuple[int, int, str, datetime], datetime] = {}


@dataclass
class Evidence:
    """One attack step: when, its techniques, what names it, the findings cited on it."""

    at: datetime
    techniques: Set[str]
    label: str
    finding_ids: List[str]
    hostname: Optional[str] = None
    run_id: Optional[str] = None


@dataclass
class Miss:
    """An emulated step no rule caught: the run, its technique and host."""

    at: datetime
    technique: str
    hostname: str
    run_id: str


@dataclass
class Step:
    """A scored emulated step with a host and a window: what a shadow copy is searched over."""

    at: datetime
    technique: str
    hostname: str
    run_id: str
    start: datetime
    end: datetime


@dataclass
class CaughtReport:
    source: str = "caught"
    runs: int = 0
    verdicts: int = 0
    findings: int = 0
    missed: int = 0
    written: int = 0
    failed: int = 0
    # Shadow copies searched over an emulated step's window, and those credited.
    searched: int = 0
    shadow: int = 0
    # Verdicts were not read this sync (HOLD_SECONDS); emulation runs always are.
    held: bool = False
    error: Optional[str] = None


def _parent(technique: str) -> str:
    return technique.strip().upper().split(".")[0]


def _cited(steps: Iterable[Any]) -> List[str]:
    return [
        str(c["finding_id"])
        for step in steps
        if isinstance(step, dict)
        for c in step.get("citations") or []
        if isinstance(c, dict)
        and c.get("finding_id")
        and c.get("data_source") not in _SKIPPED
    ]


def _utc(value: Any) -> datetime:
    when = catalog._when(value)
    return when or datetime.min.replace(tzinfo=timezone.utc)


def resolve_cited(
    session: Any, finding_ids: Iterable[str]
) -> Dict[str, Dict[str, Any]]:
    """Each finding id's ``catalog.resolve_findings`` row (resolved or not)."""
    ids = set(finding_ids)
    if not ids:
        return {}
    findings = {
        fid: {
            "data_source": source,
            "timestamp": ts,
            "entity_context": {"source_evidence": found, "detection_rule": named},
        }
        for fid, source, ts, found, named in session.execute(
            select(
                Finding.finding_id,
                Finding.data_source,
                Finding.timestamp,
                Finding.entity_context["source_evidence"],
                Finding.entity_context["detection_rule"],
            ).where(Finding.finding_id.in_(ids))
        )
    }
    return catalog.resolve_findings(session, findings, full=False)


def _from_runs(
    start: datetime, end: datetime
) -> Tuple[List[Evidence], List[Miss], List[Step]]:
    """Each caught and each missed step of each finished compose run, by host, and
    each scored step with a host and window (whatever its verdict)."""
    from core.memory.learning_episodes import emulation_episodes

    caught, missed, steps = [], [], []
    for episode in asyncio.run(emulation_episodes(start, end)):
        at = _utc(episode.get("concluded_at"))
        run_id = episode["investigation_id"]
        for row in episode["payload"].get("techniques") or []:
            for step in row.get("steps") or []:
                host = step.get("hostname")
                verdict = step.get("verdict")
                began, ended = (
                    catalog._when(step.get(k)) for k in ("started_at", "ended_at")
                )
                if verdict != "unknown" and host and began and ended:
                    steps.append(
                        Step(at, row["technique_id"], host, run_id, began, ended)
                    )
                if verdict in ("rule", "both"):
                    on = f" on {host}" if host else ""
                    caught.append(
                        Evidence(
                            at,
                            {row["technique_id"]},
                            f"emulation {run_id} (compose run{on})",
                            _cited([step]),
                            host,
                            run_id,
                        )
                    )
                elif verdict in ("missed", "loglm") and host:
                    missed.append(Miss(at, row["technique_id"], host, run_id))
    return caught, missed, steps


def _from_verdicts(since_days: int) -> List[Evidence]:
    """Each proven Verdict as one step, as ``missed_runs_report`` reconstructs it."""
    from core.storage.database_data_service import DatabaseDataService

    data = DatabaseDataService()

    def read(start: datetime, end: datetime) -> List[Dict[str, Any]]:
        return data.get_findings(
            limit=gaps.FINDINGS_LIMIT, timestamp_start=start, timestamp_end=end
        )

    out = []
    for v in gaps.read_ledger(since_days):
        if v["outcome"] != "proven" or not v["techniques"]:
            continue
        why, record = gaps.reconstruct_verdict(v, read)
        if why:
            continue
        kind = "Case" if v["investigation_kind"] == "case" else "hunt"
        out.append(
            Evidence(
                _utc(v["concluded_at"]),
                set(v["techniques"]),
                f"{kind} {v['investigation_id']} (proven, {v['trust']})",
                _cited([record]),
            )
        )
    return out


def _day(at: datetime) -> str:
    return f", {at:%Y-%m-%d}" if at.year > 1 else ""


def judge(
    session: Any, evidence: List[Evidence], misses: Iterable[Miss] = ()
) -> Dict[Tuple[int, int], Tuple[str, Dict[str, Any]]]:
    """Per (rule id, version) a new Caught result: its rule's source and the result.

    The newest evidence names a version. A pass is written on a version not
    already passing whose stored result is older than the catch; a fail on a
    rule's latest version whose stored pass is an emulation of a missed step's
    technique on its host, older than the miss, when no version of that rule
    caught a step at or after the miss.
    """
    resolved = {
        fid: (r["rule_id"], r["version"])
        for fid, r in resolve_cited(
            session, {fid for e in evidence for fid in e.finding_ids}
        ).items()
        if r["resolved"]
    }
    versions = (
        {
            (rid, ver): (source, {_parent(t) for t in techniques or []}, caught)
            for rid, ver, source, techniques, caught in session.execute(
                select(
                    DetectionRuleVersion.rule_id,
                    DetectionRuleVersion.version,
                    DetectionRule.source,
                    DetectionRule.techniques,
                    DetectionRuleVersion.checks[CHECK],
                )
                .join(DetectionRule, DetectionRule.id == DetectionRuleVersion.rule_id)
                .where(
                    tuple_(
                        DetectionRuleVersion.rule_id, DetectionRuleVersion.version
                    ).in_(set(resolved.values())),
                    _JUDGED,
                )
            )
        }
        if resolved
        else {}
    )
    out: Dict[Tuple[int, int], Tuple[str, Dict[str, Any]]] = {}
    # The newest catch per version, whether or not it is written.
    newest: Dict[Tuple[int, int], datetime] = {}
    # The newest catch by any version of each rule: a flag is per rule.
    caught_rule: Dict[int, datetime] = {}
    for e in sorted(evidence, key=lambda e: e.at, reverse=True):
        for fid in e.finding_ids:
            key = resolved.get(fid)
            if key is None or key in newest or key not in versions:
                continue
            source, declared, stored = versions[key]
            technique = next(
                (t for t in sorted(e.techniques) if _parent(t) in declared), None
            )
            if technique is None:
                continue
            newest[key] = e.at
            caught_rule[key[0]] = max(caught_rule.get(key[0], e.at), e.at)
            stored = stored if isinstance(stored, dict) else {}
            if stored.get("result") == "pass":
                continue
            if stored.get("at") and _utc(stored["at"]) >= e.at:
                continue  # a fail newer than this catch stands
            result = {
                "result": "pass",
                "reason": f"pass: caught in {e.label}, {technique}{_day(e.at)}",
                "technique": technique,
                "at": e.at.isoformat(),
            }
            if e.hostname:
                result["hostname"] = e.hostname
            if e.run_id:
                result["run_id"] = e.run_id
            out[key] = (source, result)

    misses = sorted(misses, key=lambda m: m.at, reverse=True)
    if not misses:
        return out
    held = DetectionRuleVersion.checks[CHECK]
    stage = catalog.CURRENT_STAGE
    for rid, ver, source, stored in session.execute(
        select(
            DetectionRuleVersion.rule_id,
            DetectionRuleVersion.version,
            DetectionRule.source,
            held,
        )
        .join(DetectionRule, DetectionRule.id == DetectionRuleVersion.rule_id)
        .outerjoin(stage, stage.c.rule_id == DetectionRule.id)
        .where(
            _JUDGED,
            # A miss is no live rule alerting: it says nothing of a copy still in
            # shadow whose pass came from its own search.
            or_(
                held["shadow"].astext.is_(None),
                stage.c.stage.is_distinct_from("shadow"),
            ),
            # Only the version the rule runs now: a flag on an old one never clears.
            DetectionRuleVersion.version == DetectionRule.latest_version,
            held["result"].astext == "pass",
            held["hostname"].astext.isnot(None),
            held["technique"].astext.isnot(None),
        )
    ):
        key = (rid, ver)
        passed_at = _utc(stored.get("at"))
        miss = next(
            (
                m
                for m in misses
                if _parent(m.technique) == _parent(stored["technique"])
                and m.hostname.casefold() == str(stored["hostname"]).casefold()
                and m.at > passed_at
            ),
            None,
        )
        if miss is None or caught_rule.get(rid, passed_at) >= miss.at:
            continue
        out[key] = (
            source,
            {
                "result": "fail",
                "reason": (
                    f"fail: missed in emulation {miss.run_id} (compose run on "
                    f"{miss.hostname}), {miss.technique}{_day(miss.at)}"
                ),
                "technique": miss.technique,
                "hostname": miss.hostname,
                "run_id": miss.run_id,
                "at": miss.at.isoformat(),
            },
        )
    return out


def _siem(language: str) -> Optional[Tuple[str, int, Any, Any]]:
    """(budget integration, per hour, service, rollout's query) a copy of this
    language is searched with, or None when its SIEM is not configured."""
    from core.detections.inventory import kibana, rollout, splunk, wazuh_sees

    if language == "splunk":
        config = rollout._splunk_config()
        if config is None:
            return None
        return (
            splunk.SOURCE,
            splunk.per_hour_of(config),
            splunk.service_of(config),
            rollout._splunk_query,
        )
    if language == "elastic":
        config = rollout._elastic_config()
        query = rollout._query
    else:  # Wazuh: its archive on elastic-siem's indexer and budget
        why, config = wazuh_sees.indexer()
        config = None if why else config
        query = rollout._wazuh_query
    if config is None:
        return None
    per_hour = config.get("max_searches_per_hour")
    per_hour = sees.DEFAULT_PER_HOUR if per_hour is None else max(int(per_hour), 0)
    return rollout.INTEGRATION, per_hour, kibana.service_of(config), query


def _events(siem: str, doc: Mapping[str, Any]) -> List[Any]:
    """A hit as Events: an EQL sequence hit is each of its events."""
    from core.detections.engineering import siem_replay

    seq = (doc.get("_source") or {}).get("sequence")
    if isinstance(seq, list):
        return [siem_replay.event_of(siem, {"_source": e}) for e in seq if e]
    return [siem_replay.event_of(siem, doc)]


async def _search(
    language: str, siem: Tuple[str, int, Any, Any], pairs: List[Tuple[Any, List[Step]]]
) -> Tuple[int, Dict[Tuple[int, int], Dict[str, Any]]]:
    """Each copy searched over its steps' windows, newest first, until one holds a hit."""
    from core.detections.engineering import siem_replay
    from core.integrations._base import search_budget

    integration, per_hour, service, query = siem
    searched, out = 0, {}
    try:
        for copy, steps in pairs:
            for step in steps:
                claim = siem_replay.claim_of(
                    f"emulation:{step.run_id}",
                    {"hostnames": [step.hostname]},
                    step.start,
                    step.end,
                )
                # Spent: the rest wait for a later sync. A key apart from rollout's.
                if not search_budget.spend(
                    integration, per_hour, f"caught:{copy.rule_id}"
                ):
                    return searched, out
                searched += 1
                try:
                    _, docs = await query(service, copy, claim.start, claim.end)
                except Exception as exc:  # noqa: BLE001 -- untested this tick
                    logger.warning(
                        "detection rules: Caught shadow search of %s failed: %s",
                        copy.name,
                        exc,
                    )
                    continue
                _answered[(copy.rule_id, copy.version, step.run_id, step.start)] = (
                    step.at
                )
                if not any(claim.holds(e) for d in docs for e in _events(language, d)):
                    continue
                out[(copy.rule_id, copy.version)] = {
                    "result": "pass",
                    "reason": (
                        f"pass: caught in shadow in emulation {step.run_id} (compose "
                        f"run on {step.hostname}), {step.technique}{_day(step.at)}"
                    ),
                    "technique": step.technique,
                    "hostname": step.hostname,
                    "run_id": step.run_id,
                    "at": step.at.isoformat(),
                    "shadow": True,
                }
                break
    finally:
        close = getattr(service, "close", None)
        if close is not None and asyncio.iscoroutinefunction(close):
            await close()
    return searched, out


def shadow_proof(
    steps: List[Step], taken: Set[Tuple[int, int]], report: CaughtReport
) -> Dict[Tuple[int, int], Dict[str, Any]]:
    """A pass per shadow overlay copy whose search over a matching emulated step's
    window returned an event on that step's host. ``taken`` (versions judge()
    already wrote) are not searched."""
    from core.detections.inventory import rollout, wazuh_shadow

    if not steps or rollout._stopped():  # Stop halts Vigil's own searches
        return {}
    copies = [
        c
        for c in rollout._copies()
        if c.stage == "shadow"
        and (c.checks.get(CHECK) or {}).get("result") != "pass"
        and (c.rule_id, c.version) not in taken
        and (
            not rollout.runnable(c.rule)
            if c.language == "elastic"
            else (
                str(c.rule.get("search") or "").strip()
                if c.language == "splunk"
                else not wazuh_shadow.translate(c.toml).why
            )
        )
    ]
    if not copies:
        return {}
    skip = rollout.handed_off([c.rule_id for c in copies])
    with get_db_manager().session_scope() as session:
        declared = {
            rid: {_parent(t) for t in techniques or []}
            for rid, techniques in session.execute(
                select(DetectionRule.id, DetectionRule.techniques).where(
                    DetectionRule.id.in_([c.rule_id for c in copies])
                )
            )
        }
    newest = sorted(steps, key=lambda s: s.at, reverse=True)
    oldest = min(s.at for s in steps)
    for k in [k for k, at in _answered.items() if at < oldest]:
        del _answered[k]  # its run has left RUNS_WINDOW
    by_language: Dict[str, List[Tuple[Any, List[Step]]]] = {}
    for c in copies:
        if c.rule_id in skip:
            continue
        stored = c.checks.get(CHECK) or {}
        fail_at = _utc(stored.get("at")) if stored.get("at") else None
        mine = [
            s
            for s in newest
            if _parent(s.technique) in declared.get(c.rule_id, set())
            and (fail_at is None or s.at > fail_at)  # a newer fail stands
            and (c.rule_id, c.version, s.run_id, s.start) not in _answered
        ]
        if mine:
            by_language.setdefault(c.language, []).append((c, mine))
    out: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for language, pairs in by_language.items():
        try:  # one SIEM unreachable leaves the others' searches standing
            siem = _siem(language)
            if siem is None:
                continue
            searched, found = asyncio.run(_search(language, siem, pairs))
        except Exception as exc:  # noqa: BLE001 -- untested this tick
            logger.warning(
                "detection rules: Caught %s shadow search failed: %s", language, exc
            )
            continue
        report.searched += searched
        out.update(found)
    return out


def sweep() -> CaughtReport:
    """Credit each rule version cited on a matching attack step, and fail one an
    emulation shows no longer catches. Verdicts are read at most once per hold.
    A shadow copy is searched once per matching emulated step, from its SIEM's
    budget, and credited when a hit is on the step's host in its window."""
    global _last_swept
    report = CaughtReport()
    due = _last_swept is None or time.monotonic() - _last_swept >= HOLD_SECONDS
    report.held = not due
    try:
        end = datetime.now(timezone.utc)
        runs, misses, steps = _from_runs(end - RUNS_WINDOW, end)
        verdicts = _from_verdicts(LOOKBACK_DAYS) if due else []
        report.runs = len({e.run_id for e in runs} | {m.run_id for m in misses})
        report.missed = len(misses)
        report.verdicts = len(verdicts)
        evidence = runs + verdicts
        report.findings = len({fid for e in evidence for fid in e.finding_ids})
        with get_db_manager().session_scope() as session:
            caught = judge(session, evidence, misses)
        # judge()'s result for a version wins over a shadow search's.
        try:
            shadow = shadow_proof(steps, set(caught), report)
        except Exception as exc:  # noqa: BLE001 -- judge()'s results still write
            logger.warning("detection rules: Caught shadow proof failed: %s", exc)
            report.error = f"shadow: {type(exc).__name__}: {exc}"
            shadow = {}
        report.shadow = len(shadow)
        for key, result in shadow.items():
            caught.setdefault(key, (readers.OVERLAY_SOURCE, result))
        by_source: Dict[str, Dict[Tuple[int, int], Dict[str, Any]]] = {}
        for key, (source, result) in caught.items():
            by_source.setdefault(source, {})[key] = result
        for source, results in by_source.items():
            report.written += sees.write_check(CHECK, results, source=source)
        report.failed = sum(r["result"] == "fail" for _, r in caught.values())
        if due:
            _last_swept = time.monotonic()
    except Exception as exc:  # noqa: BLE001 -- the sync's own reports stand
        logger.warning("detection rules: Caught sweep failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Caught %s", report)
    return report
