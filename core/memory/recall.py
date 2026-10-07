"""Reading episodic memory (#732): an entity's history, and the log of the read.

One query serves both callers. A hunt worker calls ``recall_entity`` mid-run and
gets the mapping as the single row of a ToolResult; the run-start read hands the
same function the keys its harness extracted and journals the same mapping as the
recall event. A second query for the second caller would be a second contract,
and the second one drifts.

Three things the query has to get right, and each of them fails quietly rather
than loudly if it does not.

**A total order.** ``LIMIT`` over a partial order lets Postgres return a
different set on identical data. Nothing errors; the run simply remembers
something else, and it surfaces a release later as a replay diff. Ties break on
the primary key, which is what :data:`RECALL_ORDER` says and what the recall
indexes are built for.

**A per-key cap before the overall budget, as a LATERAL.** A trailing ``LIMIT``
lets one entity present in every hunt -- a resolver, a domain controller -- spend
the whole budget on itself, which makes memory *less* useful the more of it there
is. The cap is applied per key and per kind, inside a lateral, before the overall
cap sees a row.

**The dropped count is reported.** A caller shown a partial view has no way to
tell it apart from an entity with a short history unless it is told.

``concluded_at <= as_of`` is a freshness filter and nothing more, never a
substitute for logging the rows. ``27_episodic_read_log.sql`` says why on the
column that carries it.

The log is written here, inside the query, rather than at either call site, so a
read through ``/internal/tools/invoke`` and a direct ``execute_backend_tool``
leave the same record. What it is for, and why it alone is retained, is stated on
the table in ``infra/database/init/27_episodic_read_log.sql``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from sqlalchemy import Text as SAText
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY as PGArray
from sqlalchemy.orm import Session

from core.memory.entity_keys import normalise_keys
from core.memory.recall_contract import (
    DROPPED_KINDS,
    RECALL_IGNORED_ARGS,
    RECALL_KEY_ARGS,
    RECALL_ORDER,
    RECALL_OVERALL_CAP,
    RECALL_PER_KEY_CAP,
    RECALL_TOOL,
    UNKNOWN_CALLER,
    empty_dropped,
    recall_ranking,
)
from core.storage.unit_of_work import unit_of_work

Args = Dict[str, Any]
Row = Dict[str, Any]

# The kinds, in the order a result presents them, and the keys of `dropped` and
# `row_counts` too -- the contract's tuple rather than a second copy of it, so a
# kind added there cannot be one this module quietly stops returning.
KINDS: Tuple[str, ...] = DROPPED_KINDS


# RECALL_ORDER as (field, descending) pairs. Both the SQL below and the overall
# cap in Python sort on these, because the result and the read log report
# RECALL_ORDER as the basis the rows were chosen on -- and a step that restated
# the order instead of deriving it would go on keeping the old rows while the
# result reported the new constant, which is the drift this exists to prevent.
_ORDER_TERMS: Tuple[Tuple[str, bool], ...] = tuple(
    (term.split()[0], term.split()[-1].upper() == "DESC")
    for term in RECALL_ORDER.split(",")
)

# The term the kind has to outrank; see _apply_overall_cap.
_PRIMARY_KEY = "id"


def _order_by(alias: str) -> str:
    """RECALL_ORDER as a clause on one alias."""
    return ", ".join(f"{alias}.{term.strip()}" for term in RECALL_ORDER.split(","))


# One statement per kind, and two laterals in it: how many distinct rows matched
# at all, and the per-key capped page of them. Two statements would read two
# snapshots under READ COMMITTED, and the dropped count would then describe a set
# nobody was shown.
#
# The total counts distinct rows over *all* the keys, not per key. A Verdict
# naming two of the queried keys matches twice and is returned once, so a per-key
# sum would report ten rows withheld where five distinct conclusions were.
#
# The outer ORDER BY is not decoration. Without it the join order is Postgres's
# to choose, and the dedup in _page -- that same Verdict, arriving twice -- would
# keep whichever copy came first.
def _statement(table: str, predicate: str, matches_any: str, columns: str) -> Any:
    return text(f"""
        SELECT m.total AS matched_total, {columns}
        FROM unnest(CAST(:keys AS text[])) AS k(key)
        CROSS JOIN LATERAL (
            SELECT count(*) AS total
            FROM {table} AS c
            WHERE {matches_any.format(alias="c")} AND c.concluded_at <= :as_of
        ) AS m
        CROSS JOIN LATERAL (
            SELECT *
            FROM {table} AS p
            WHERE {predicate.format(alias="p")} AND p.concluded_at <= :as_of
            ORDER BY {_order_by("p")}
            LIMIT :per_key_cap
        ) AS r
        ORDER BY {_order_by("r")}, k.key ASC
        """).bindparams(bindparam("keys", type_=PGArray(SAText)))


# Sightings carry the key on a column. Verdicts and Gaps name their subjects in
# an array -- typically one to three entities, not the 38 to 76 their evidence
# touched (ADR 0016) -- and the containment operator is what the GIN indexes on
# subject_entities answer.
_BY_KEY = "{alias}.entity_key = k.key"
_BY_SUBJECT = "{alias}.subject_entities @> ARRAY[k.key]"

# The same joins against the whole key list at once, for the distinct total the
# per-key cap is measured against. Both are the indexed forms: `= ANY` uses the
# recall index and the overlap operator uses the GIN one.
_ANY_KEY = "{alias}.entity_key = ANY(CAST(:keys AS text[]))"
_ANY_SUBJECT = "{alias}.subject_entities && CAST(:keys AS text[])"

_SIGHTING_COLUMNS = (
    "r.id, r.entity_key, r.investigation_kind, r.investigation_id, "
    "r.source_system, r.hit_count, r.attacker_influenceable, "
    "r.first_seen, r.last_seen, r.concluded_at"
)
_VERDICT_COLUMNS = (
    "r.id, r.investigation_kind, r.investigation_id, r.hypothesis_id, "
    "r.statement, r.outcome, r.rationale, r.subject_entities, "
    "r.attacker_influenceable_only, r.trust, r.first_seen, r.last_seen, "
    "r.window_source, r.concluded_at"
)
_GAP_COLUMNS = (
    "r.id, r.investigation_kind, r.investigation_id, r.hypothesis_id, "
    "r.statement, r.disposition, r.reason, r.subject_entities, r.concluded_at"
)

_QUERIES = {
    "sightings": _statement("episodic_sightings", _BY_KEY, _ANY_KEY, _SIGHTING_COLUMNS),
    "verdicts": _statement(
        "episodic_verdicts", _BY_SUBJECT, _ANY_SUBJECT, _VERDICT_COLUMNS
    ),
    "gaps": _statement("episodic_gaps", _BY_SUBJECT, _ANY_SUBJECT, _GAP_COLUMNS),
}

# Fetched for the Verdicts that survived both caps rather than joined into the
# page above, which would multiply a Verdict by its sources and spend the per-key
# cap on copies of one conclusion.
_VERDICT_SOURCES = text("""
    SELECT verdict_id, source_system, stance, source_tier
    FROM episodic_verdict_sources
    WHERE verdict_id = ANY(CAST(:verdict_ids AS bigint[]))
    ORDER BY verdict_id ASC, source_system ASC
    """)

_LOG_READ = text("""
    INSERT INTO episodic_read_log
        (caller_kind, caller_id, keys, as_of, row_counts, dropped, ranking)
    VALUES
        (:caller_kind, :caller_id, CAST(:keys AS text[]), :as_of,
         CAST(:row_counts AS jsonb), CAST(:dropped AS jsonb), CAST(:ranking AS jsonb))
    """).bindparams(bindparam("keys", type_=PGArray(SAText)))


_EXPIRE_READ_LOG = text("DELETE FROM episodic_read_log WHERE ts < :cutoff")


def _iso(value: Any) -> Any:
    """Timestamps travel as ISO-8601 with an offset, matching the contract."""
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value


# What tools_router._is_bad_arguments matches on, so a mistake the caller can fix
# comes back as invalid_args and not backend_error -- the contract keeps "the call
# was wrong" apart from "the tool could not answer", and only the first tells the
# model to try again. Coupling to the router's wording is fragile in both
# directions; it is the router's contract, and stating it once here is better than
# two error classes for two spellings of the same caller mistake.
def _refuse(detail: str) -> TypeError:
    return TypeError(f"{RECALL_TOOL}: {detail} (required keyword-only argument)")


def _as_of(raw: Any) -> datetime:
    """The freshness filter, defaulting to now.

    A naive value is read as UTC rather than refused: every timestamp this tier
    stores is offset-aware, so a caller that dropped the offset meant UTC, and
    failing the read would tell it nothing it could act on. An unreadable one is
    the caller's mistake and is refused as such, the same as a call naming no key.
    """
    if isinstance(raw, datetime):
        moment = raw
    elif raw is None or (isinstance(raw, str) and not raw.strip()):
        return datetime.now(timezone.utc)
    else:
        stamp = str(raw).strip()
        try:
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            raise _refuse(f"as_of {stamp!r} is not ISO-8601 with an offset") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _window(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {"first_seen": _iso(row["first_seen"]), "last_seen": _iso(row["last_seen"])}


def _provenance(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "investigation_kind": row["investigation_kind"],
        "investigation_id": row["investigation_id"],
        "concluded_at": _iso(row["concluded_at"]),
    }


def _sighting(row: Mapping[str, Any]) -> Row:
    return {
        **_provenance(row),
        "entity_key": row["entity_key"],
        "source_system": row["source_system"],
        "hit_count": int(row["hit_count"]),
        "attacker_influenceable": bool(row["attacker_influenceable"]),
        "window": _window(row),
    }


def _verdict(row: Mapping[str, Any]) -> Row:
    return {
        **_provenance(row),
        "hypothesis_id": row["hypothesis_id"],
        "statement": row["statement"],
        "outcome": row["outcome"],
        "rationale": row["rationale"],
        "subject_entities": list(row["subject_entities"] or []),
        "attacker_influenceable_only": bool(row["attacker_influenceable_only"]),
        "trust": row["trust"],
        "window": _window(row),
        "window_source": row["window_source"],
        # Filled once the overall cap has said which Verdicts survive.
        "sources": [],
    }


def _gap(row: Mapping[str, Any]) -> Row:
    return {
        **_provenance(row),
        "hypothesis_id": row["hypothesis_id"],
        "statement": row["statement"],
        "disposition": row["disposition"],
        "reason": row["reason"],
        "subject_entities": list(row["subject_entities"] or []),
    }


_SHAPES = {"sightings": _sighting, "verdicts": _verdict, "gaps": _gap}


class _Selected(NamedTuple):
    """A chosen row, beside the two values the total order sorts it on.

    They sit alongside the row rather than in it because neither belongs in the
    result: ``id`` is per-table and would collide across kinds, and
    ``concluded_at`` is already on the row as a string. Named rather than a bare
    tuple so :func:`_apply_overall_cap` can reach the field RECALL_ORDER
    nominates by name instead of by position.
    """

    concluded_at: datetime
    id: int
    row: Row


def _page(
    session: Session, kind: str, params: Mapping[str, Any]
) -> Tuple[List[_Selected], int]:
    """One kind's rows, per-key capped, deduped, and what the cap withheld.

    A Verdict or Gap naming two of the queried keys matches twice and is returned
    once; the first copy wins, which the statement's ORDER BY makes a decision
    rather than an accident.

    The drop count is distinct rows matched minus distinct rows returned, and not
    a per-key sum of overshoot -- that same shared Verdict would otherwise be
    counted as withheld from each key it names, reporting ten rows lost where
    five conclusions were. Zero rows matched means the total was never observed,
    which is the same thing as nothing having been withheld.
    """
    shape = _SHAPES[kind]
    selected: List[_Selected] = []
    seen: set = set()
    matched = 0

    for row in session.execute(_QUERIES[kind], dict(params)).mappings():
        # Constant across every row -- the lateral that computes it joins on the
        # whole key list and is uncorrelated -- so this reads the same value each
        # time rather than accumulating one.
        matched = int(row["matched_total"])
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        selected.append(_Selected(row["concluded_at"], int(row["id"]), shape(row)))

    return selected, max(0, matched - len(selected))


def _apply_overall_cap(
    pages: Mapping[str, List[_Selected]],
) -> Tuple[Dict[str, List[_Selected]], Dict[str, int]]:
    """Spend the overall cap across all kinds and keys, newest first.

    One budget rather than one per kind, which is what the contract's
    ``overall_cap`` says. This is the step that decides which rows a caller
    actually sees, so it sorts on :data:`_ORDER_TERMS` -- RECALL_ORDER parsed --
    and not on directions written out here. Restating them would leave the
    laterals following the constant while the budget kept the opposite rows, and
    the result would go on reporting the constant as the basis for a set chosen
    some other way.

    Ordering across kinds needs one term more than RECALL_ORDER has. Ids are
    per-table, so a Sighting and a Gap sharing a ``concluded_at`` and an ``id``
    are a tie it cannot break, and an unbroken tie is the nondeterminism the
    total order exists to prevent. The kind breaks it, and it is applied directly
    above the primary key -- the term it exists to make meaningful -- rather than
    appended, which would leave it below the tie it is there to settle.

    That extra term decides *which* rows the budget keeps and never the order
    they are presented in: each returned list is one kind, and within a kind the
    order is RECALL_ORDER exactly as the result and the log report it.

    One stable sort per term, applied least-significant first, rather than one
    key over the whole tuple: the terms differ in direction, and reversing just
    one of them by negating it would need a number -- which for ``concluded_at``
    means a float epoch, turning two rows a microsecond apart into a tie.
    """
    ordered = [(kind, selected) for kind in KINDS for selected in pages[kind]]

    for field, descending in reversed(_ORDER_TERMS):
        ordered.sort(
            key=lambda entry, f=field: getattr(entry[1], f), reverse=descending
        )
        if field == _PRIMARY_KEY:
            ordered.sort(key=lambda entry: entry[0])

    kept: Dict[str, List[_Selected]] = {kind: [] for kind in KINDS}
    for kind, selected in ordered[:RECALL_OVERALL_CAP]:
        kept[kind].append(selected)

    dropped = {kind: len(pages[kind]) - len(kept[kind]) for kind in KINDS}
    return kept, dropped


def _attach_sources(session: Session, verdicts: Sequence[_Selected]) -> None:
    """Give each surviving Verdict its per-source Stance and stamped Source Tier."""
    by_id = {selected.id: selected.row for selected in verdicts}
    if not by_id:
        return
    for row in session.execute(
        _VERDICT_SOURCES, {"verdict_ids": sorted(by_id)}
    ).mappings():
        by_id[row["verdict_id"]]["sources"].append(
            {
                "source_system": row["source_system"],
                "stance": row["stance"],
                "source_tier": row["source_tier"],
            }
        )


def _log(
    session: Session,
    result: Mapping[str, Any],
    as_of: datetime,
    caller_kind: str,
    caller_id: str,
) -> None:
    """Record that the read happened, whoever asked.

    In the same transaction as the read and not best-effort. "Every read is
    logged" is the whole claim this table exists to make, and a read that
    answered without leaving a record is indistinguishable afterwards from a read
    nobody made. A failure here means the table is gone or the database is down,
    in which case the query above would not have answered either.
    """
    session.execute(
        _LOG_READ,
        {
            "caller_kind": caller_kind,
            "caller_id": caller_id,
            "keys": list(result["keys"]),
            "as_of": as_of,
            "row_counts": json.dumps({kind: len(result[kind]) for kind in KINDS}),
            "dropped": json.dumps(result["dropped"]),
            "ranking": json.dumps(result["ranking"]),
        },
    )


def recall(
    keys: Iterable[str],
    *,
    as_of: Any = None,
    caller_kind: str = UNKNOWN_CALLER,
    caller_id: str = UNKNOWN_CALLER,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """What is remembered about these entities, and a log row saying it was asked.

    Unknown keys are empty lists and zeros rather than an error: an entity nobody
    has investigated and an entity that does not exist are the same answer, and
    there is nothing a caller could do differently about either.

    Passing ``session`` joins the caller's transaction, and ``unit_of_work`` then
    neither commits nor closes it. The log row is written into that transaction,
    so committing it becomes the caller's obligation -- a caller that rolls back
    has un-logged the read it performed. Only the tests pass one today.
    """
    normalised = normalise_keys(list(keys))
    moment = _as_of(as_of)
    result: Dict[str, Any] = {
        "keys": normalised,
        "as_of": _iso(moment),
        "sightings": [],
        "verdicts": [],
        "gaps": [],
        "dropped": empty_dropped(),
        "ranking": recall_ranking(),
    }

    with unit_of_work(session) as db:
        if normalised:
            params = {
                "keys": normalised,
                "as_of": moment,
                "per_key_cap": RECALL_PER_KEY_CAP,
            }
            pages: Dict[str, List[_Selected]] = {}
            for kind in KINDS:
                pages[kind], over = _page(db, kind, params)
                result["dropped"][kind]["per_key_cap"] = over

            kept, spent = _apply_overall_cap(pages)
            for kind in KINDS:
                result[kind] = [selected.row for selected in kept[kind]]
                result["dropped"][kind]["overall_cap"] = spent[kind]

            _attach_sources(db, kept["verdicts"])

        _log(db, result, moment, caller_kind, caller_id)

    return result


def recall_entity(args: Args) -> Dict[str, Any]:
    """The backend tool, taking an args mapping rather than keyword parameters.

    ``tools_router._bounded`` injects ``limit`` into every backend call, so an
    explicit signature would answer ``invalid_args`` to every recall. That bound
    is ignored here on purpose -- the caps are memory's own, and a row budget
    meant for a page of findings is not one for an entity's history.
    """
    supplied = dict(args or {})
    for ignored in RECALL_IGNORED_ARGS:
        supplied.pop(ignored, None)

    keys = supplied.get("entity_keys")
    if isinstance(keys, str):
        keys = [keys]
    if not keys:
        single = supplied.get("entity_key")
        keys = [single] if single else []
    if not keys:
        raise _refuse("a call needs " + " or ".join(RECALL_KEY_ARGS))

    return recall(
        keys,
        as_of=supplied.get("as_of"),
        caller_kind=str(supplied.get("caller_kind") or UNKNOWN_CALLER),
        caller_id=str(supplied.get("caller_id") or UNKNOWN_CALLER),
    )


def expire_read_log(cutoff: datetime, *, session: Optional[Session] = None) -> int:
    """Delete read log rows older than ``cutoff``, returning how many went.

    Called by the daemon's cleanup sweep with ``scheduler.cleanup_retention_days``
    rather than a setting of its own: a second knob would be a second thing
    to get wrong.
    """
    with unit_of_work(session) as db:
        return int(db.execute(_EXPIRE_READ_LOG, {"cutoff": cutoff}).rowcount or 0)
