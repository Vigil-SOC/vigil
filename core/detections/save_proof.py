"""Prove a rule at save on the SIEM's own events, not the caller's (#4212).

The caller names the attack events by id per SIEM, as its SIEM tools returned
them (a Splunk row's ``_bkt:_cd``, or ``_cd`` alone; an Elasticsearch hit's
``_id``). Vigil reads them itself, so an event the caller wrote proves nothing.

- **Sigma form**: replayed with ``siem_replay`` on the named events and on the
  window's normal events (events no Case or hunt claims, less those a Sigma
  exception matches). Refused when it hits none of the named events or matches
  a normal one. A normal read cut short, or one with no event holding a field
  the rule reads, is kept as a caveat: there is no person here to give a reason.
- **Each Splunk and Elastic copy**: run in its SIEM as shadow runs it
  (``rollout._splunk_query`` / ``rollout._query``), over the hits' own window.
  Refused when it returns events but none of the named ones. A search whose rows
  carry no event id (a transforming search, an ES|QL query) or whose read was
  cut short can't be judged here: it saves, unproven.
- **A Wazuh copy** has no on-demand run: it saves, unproven, and its shadow
  stays its proof (``wazuh_shadow``).

Each copy's result is stored as ``checks.replay`` with ``source: siem``. Only a
``pass`` from here, or ``caught``, lets the sweep promote a copy (``rollout._due``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.detections.engineering import siem_replay
from core.detections.inventory import rollout
from core.detections.inventory.record import RuleRecord

logger = logging.getLogger(__name__)

SOURCE = "siem"
SIEMS = ("splunk", "elastic")
MAX_WINDOW = timedelta(days=365)
# The hits' own window, widened: a copy's time field may stamp a little apart.
PAD = timedelta(hours=1)


class Refused(ValueError):
    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.detail = detail


def named_ids(given: Any) -> Dict[str, List[str]]:
    """``proven_event_ids`` as {siem: [id, ...]}; Refused when it is malformed."""
    if not isinstance(given, Mapping):
        raise Refused(
            'proven_event_ids must be an object keyed by SIEM: {"splunk": [...], '
            '"elastic": [...]}'
        )
    out: Dict[str, List[str]] = {}
    for siem, ids in given.items():
        if siem not in SIEMS:
            raise Refused(
                f"proven_event_ids names {siem!r}: Vigil reads named events from "
                f"{' and '.join(SIEMS)} only"
            )
        if not isinstance(ids, list) or not all(
            isinstance(i, str) and i.strip() for i in ids
        ):
            raise Refused(f"proven_event_ids.{siem} must be a list of event ids")
        if ids:
            out[siem] = [i.strip() for i in ids]
    if not out:
        raise Refused("proven_event_ids names no event")
    return out


def window(
    started_at: Optional[str], ended_at: Optional[str], now: Optional[datetime] = None
) -> Tuple[datetime, datetime]:
    """[start, end] the named events are read in: the given bounds, else the last
    ``siem_replay.WINDOW_DAYS``."""

    def at(value: Optional[str], name: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise Refused(f"{name} is not an ISO time: {value!r}") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    end = at(ended_at, "attack_ended_at") or now or datetime.now(timezone.utc)
    start = at(started_at, "attack_started_at") or end - timedelta(
        days=siem_replay.WINDOW_DAYS
    )
    if start >= end:
        raise Refused("attack_started_at must be before attack_ended_at")
    if end - start > MAX_WINDOW:
        raise Refused(f"the attack window may span {MAX_WINDOW.days} days at most")
    return start, end


def _same(named: str, doc_id: str) -> bool:
    # A Splunk id is _bkt:_cd; a caller may name _cd alone.
    return doc_id == named or doc_id.endswith(f":{named}")


def _event_id(doc: Mapping[str, Any], language: str) -> Optional[str]:
    """The event's id, or None for a row that is no one event."""
    if language == "splunk":
        source = doc.get("_source") or {}
        return str(doc["_id"]) if source.get("_cd") else None
    return str(doc["_id"]) if doc.get("_id") else None


class _Rule:
    """What ``rollout``'s queries read off a copy."""

    def __init__(self, rule: Dict[str, Any]):
        self.rule = rule


async def _run_copy(
    language: str, rule: Dict[str, Any], start: datetime, end: datetime
) -> Tuple[Optional[int], List[Dict[str, Any]], Optional[str]]:
    """(rows, docs, why not run) of one copy over [start, end), as shadow runs it."""
    from core.integrations._base import search_budget

    if language == "splunk":
        from core.detections.inventory import splunk
        from core.integrations.splunk.descriptor import SPLUNK

        config = rollout._splunk_config()
        if config is None:
            return None, [], "Splunk is not enabled or missing its login"
        if not search_budget.spend(
            SPLUNK.id, splunk.per_hour_of(config), "save-proof:splunk"
        ):
            return None, [], "Splunk's search budget for this hour is spent"
        total, docs = await rollout._splunk_query(
            splunk.service_of(config), _Rule(rule), start, end
        )
        return total, docs, None
    from core.detections.inventory import kibana, sees

    config = rollout._elastic_config()
    if config is None:
        return None, [], "elastic-siem is not enabled or has no elasticsearch_url"
    per_hour = config.get("max_searches_per_hour")
    per_hour = sees.DEFAULT_PER_HOUR if per_hour is None else max(int(per_hour), 0)
    if not search_budget.spend(rollout.INTEGRATION, per_hour, "save-proof:elastic"):
        return None, [], "Elastic's search budget for this hour is spent"
    service = kibana.service_of(config)
    try:
        total, docs = await rollout._query(service, _Rule(rule), start, end)
    finally:
        await service.close()
    return total, docs, None


def _hits_window(evidence: Mapping[str, Any]) -> Optional[Tuple[datetime, datetime]]:
    times = [
        datetime.fromisoformat(s["at"])
        for siem in evidence.get("siems") or []
        for s in siem.get("hit_samples") or []
        if s.get("at")
    ]
    if not times:
        return None
    return min(times) - PAD, max(times) + PAD


async def _prove_sigma(
    sigma: str,
    named: Dict[str, List[str]],
    start: datetime,
    end: datetime,
    excepted: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    sources, skipped = siem_replay.configured_sources()
    sources = [s for s in sources if s.siem in named]
    # Every Verdict concluded since the window opened claims its events.
    days = max(1, (datetime.now(timezone.utc) - start).days + 1)
    claims = await asyncio.to_thread(siem_replay.ledger_claims, days)
    try:
        evidence = await siem_replay.replay(
            sigma,
            sources=sources,
            attack_claim=None,
            named=named,
            claims=claims,
            start=start,
            end=end,
            skipped=skipped,
            excepted=excepted,
        )
    finally:
        for source in sources:
            try:
                await source.close()
            except Exception as exc:  # closing must not hide the replay
                logger.warning("save proof: closing %s: %s", source.siem, exc)
    named_count = sum(len(ids) for ids in named.values())
    evidence["named_events"] = named_count
    if not evidence.get("evaluated"):
        raise Refused(
            f"sigma form could not be replayed on the SIEM: {evidence['unproven_because']}",
            replay=evidence,
        )
    if not evidence["attack_rows"]:
        raise Refused(
            f"none of the {named_count} named events could be read from the SIEM "
            f"between {start.isoformat()} and {end.isoformat()}: pass the ids exactly as "
            "the SIEM tool returned them (Splunk _bkt:_cd or _cd, Elasticsearch _id), "
            "and attack_started_at / attack_ended_at around them",
            replay=evidence,
        )
    if not evidence["hits"]:
        raise Refused(
            f"sigma form matched none of the {evidence['attack_rows']} named events "
            "read from the SIEM: write it on the fields those events carry",
            replay=evidence,
        )
    if evidence["false_positives"]:
        raise Refused(
            f"sigma form matched {evidence['false_positives']} event(s) in the window "
            "that no Case or hunt claims: narrow it, add an exception for what is "
            "expected here, or name them too if they are the attack",
            replay=evidence,
        )
    return {
        "result": "pass",
        "source": SOURCE,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        **evidence,
        "caveat": evidence.get("unproven_because"),
    }


async def _prove_copy(
    language: str,
    rule: Dict[str, Any],
    ids: Sequence[str],
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "source": SOURCE,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "named_events": len(ids),
    }
    if not ids:
        return {
            **base,
            "result": "not_run",
            "reason": f"no {language} event was named",
        }
    try:
        rows, docs, why = await _run_copy(language, rule, start, end)
    except Exception as exc:  # noqa: BLE001 -- the SIEM's own error, kept on the check
        logger.warning("save proof: %s copy run failed: %s", language, exc)
        rows, docs, why = None, [], str(exc)[:500] or type(exc).__name__
    if why is not None:
        return {**base, "result": "not_run", "reason": why}
    found = [i for i in (_event_id(d, language) for d in docs) if i]
    matched = sorted({n for n in ids if any(_same(n, f) for f in found)})
    base.update(rows=rows, matched=matched)
    if matched:
        return {**base, "result": "pass"}
    if docs and not found:
        return {
            **base,
            "result": "unverifiable",
            "reason": "its rows carry no event id (a transforming search), so they "
            "can't be matched to the named events",
        }
    if (rows or 0) >= rollout.MAX_DOCS:
        return {
            **base,
            "result": "unverifiable",
            "reason": f"it returned {rows} rows, the most one run reads, and none was "
            "a named event: a narrower window may hold it",
        }
    raise Refused(
        f"{language} copy returned {rows or 0} event(s) between {start.isoformat()} "
        f"and {end.isoformat()}, none of them the {len(ids)} named: run in {language} "
        "it would not catch this attack: check its field names against the events, "
        "and that it searches the index they are in (shadow runs it as written)",
        replay={**base, "result": "fail"},
    )


async def _prove(
    prepared: Sequence[Tuple[str, str, RuleRecord]],
    rules: Mapping[str, Dict[str, Any]],
    sigma: str,
    named: Dict[str, List[str]],
    start: datetime,
    end: datetime,
    excepted: Sequence[Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    sigma_check = await _prove_sigma(sigma, named, start, end, excepted)
    hit_start, hit_end = _hits_window(sigma_check) or (start, end)
    out: Dict[str, Dict[str, Any]] = {}
    for language, _, record in prepared:
        if language == "sigma":
            out[record.native_key] = sigma_check
        elif language == "wazuh":
            out[record.native_key] = {
                "result": "not_run",
                "source": SOURCE,
                "reason": "a Wazuh copy has no on-demand run: its shadow is its proof",
            }
        else:
            out[record.native_key] = await _prove_copy(
                language,
                rules[record.native_key],
                named.get(language, []),
                hit_start,
                hit_end,
            )
    return out


def prove(
    prepared: Sequence[Tuple[str, str, RuleRecord]],
    sigma: Optional[str],
    proven_event_ids: Any,
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    excepted: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Dict[str, Any]]:
    """``checks.replay`` per copy's native key; Refused when the SIEM does not hold the
    rule up. Blocking: called from ``save_rule``, which runs on a worker thread."""
    named = named_ids(proven_event_ids)
    if not sigma:
        raise Refused("proven_event_ids were passed, but there is no sigma form")
    start, end = window(started_at, ended_at)
    rules = {
        record.native_key: rollout._rule_of(language, record.content or {})
        for language, _, record in prepared
        if language in SIEMS
    }
    return asyncio.run(_prove(prepared, rules, sigma, named, start, end, excepted))
