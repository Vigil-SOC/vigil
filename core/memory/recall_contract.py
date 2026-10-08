"""The recall contract (#729), Python's half.

What a read of episodic memory returns, and the signature of the tool that
performs one. There is one shape, carried two ways: the run-start read journals
it verbatim as the recall event payload, and a mid-run :data:`RECALL_TOOL` call
carries the same mapping as the single row of a ``ToolResult``. A parallel
payload would be a second contract, and the second one drifts.

A mismatch between the halves fails as "no history", which is indistinguishable
from an entity nobody has looked at. The TypeScript half is
``services/agent/contracts/memory.ts`` and
``tests/unit/_ratchets/test_recall_contract_agrees.py`` fails when they
disagree — nothing calls either half yet, so a static check is the only one
available.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, Mapping, Tuple


class Trust(str, Enum):
    """Who concluded, as opposed to what the source is (**Source Tier**)."""

    ANALYST = "analyst"
    AGENT = "agent"


class Stance(str, Enum):
    """How one source bore on a **Verdict**.

    Three-valued because a flat corroborated list cannot express direction: a
    source that weakened a hypothesis and one that never bore on it are not the
    same row.
    """

    SUPPORTS = "supports"
    WEAKENS = "weakens"
    NEITHER = "neither"


class InvestigationKind(str, Enum):
    """Keyed by kind and id, never ``run_id``.

    A run-keyed schema cannot represent the Case-authored Verdicts the writer
    split requires. Wider than ``source_tier.InvestigationKind``, which selects
    a source vocabulary — an ``analyst`` investigation names no sources, and
    nothing writes one yet.
    """

    HUNT = "hunt"
    CASE = "case"
    ANALYST = "analyst"


class VerdictOutcome(str, Enum):
    """``handed_off`` is terminal for the hunt and not an ending of the claim, so
    it ranks up like a Gap while still carrying evidence, a window and sources.
    ``false_positive`` arrives only from a Case closure.
    """

    PROVEN = "proven"
    DISPROVEN = "disproven"
    INCONCLUSIVE = "inconclusive"
    HANDED_OFF = "handed_off"
    FALSE_POSITIVE = "false_positive"


class WindowSource(str, Enum):
    """A retrospective sweep over old archives asserts its window rather than
    observing it, and ranking discounts the weaker one.
    """

    OBSERVED = "observed"
    ASSERTED = "asserted"


class GapDisposition(str, Enum):
    """Why nothing was gathered.

    Each value traces to a line of the spec, because a disposition invented here
    is one #731 writes against and nothing reviews. ``DEPRIORITISED`` is the
    status map's ``parked`` arm; ``NO_EVIDENCE_GATHERED`` is its ``inconclusive``
    and ``active`` arms, and where a ``data_starved`` run's open questions land;
    ``BUDGET_EXHAUSTED`` is work abandoned for budget.

    Not ``never_dispatched``: a hypothesis that reached ``inconclusive`` with
    nothing gathered *was* dispatched. Not ``no_data_source`` either — a tool
    that could not answer is a **Visibility Gap**, which CONTEXT.md keeps
    distinct from a **Declared Gap**.
    """

    DEPRIORITISED = "deprioritised"
    NO_EVIDENCE_GATHERED = "no_evidence_gathered"
    BUDGET_EXHAUSTED = "budget_exhausted"


class DistilFailureReason(str, Enum):
    """Why a Distil did not write an investigation it was offered (#734).

    The three the Distils already count separately, because they are not the
    same problem and do not wait the same way. ``REFUSED`` is a payload this
    side will not map, and re-reading it will not change that — the fix is on
    the other side of the contract, so it waits forever rather than on a
    schedule. ``UNREADABLE`` is a fold that could not be fetched at all, which
    is usually an agent layer that is down and back shortly. ``FAILED`` is the
    write itself, which is usually the store.

    Recorded rather than only logged: a marker's absence means "not yet
    reached", and without a row saying otherwise a run that will never write is
    indistinguishable from one nothing has looked at yet.
    """

    REFUSED = "refused"
    UNREADABLE = "unreadable"
    FAILED = "failed"


RECALL_TOOL = "recall_entity"

# What the arch asks for; a ToolSpec binds it to the tool id via `provides`.
RECALL_CAPABILITY = "entity_recall"

# Keyed by the role names threathunt.yaml declares, not by a class called
# "worker" that appears in no file. Empties are decisions: the lead never
# queries, and narrative search — the lead's alone — ships no tool here.
RECALL_GRANTS: Mapping[str, Tuple[str, ...]] = {
    "lead": (),
    "critic": (),
    "threat_hunter": (RECALL_CAPABILITY,),
    "network_analyst": (RECALL_CAPABILITY,),
    "threat_intel": (RECALL_CAPABILITY,),
}

# The shape `core/llm/tool_schemas.py` already uses, so #732 registers this
# rather than transcribing it.
#
# `required` is empty on purpose. Neither key argument can be required, because
# the singular exists for the caller that sends one string *instead of* the
# list; requiring the list makes that call invalid. The invariant is
# RECALL_KEY_ARGS, checked at the call rather than by the schema.
RECALL_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Entity Keys as `type:value`, at least one",
        },
        "entity_key": {
            "type": "string",
            "description": "One Entity Key, wrapped into entity_keys",
        },
        "as_of": {
            "type": "string",
            "description": "Freshness filter, ISO-8601 with an offset; defaults to now",
        },
        "caller_kind": {"type": "string", "default": "unknown"},
        "caller_id": {"type": "string", "default": "unknown"},
    },
    "required": [],
}

# At least one, never both required. A call naming neither is invalid_args.
RECALL_KEY_ARGS: Tuple[str, ...] = ("entity_keys", "entity_key")

RECALL_ARGS: Tuple[str, ...] = tuple(RECALL_PARAMETERS["properties"])

# The handler takes an args mapping, not these as keyword parameters:
# `tools_router._bounded` injects `limit` into every backend call, and an
# explicit signature would answer invalid_args to every recall. That bound is
# not honoured either — the caps below are memory's own.
RECALL_IGNORED_ARGS: Tuple[str, ...] = ("limit",)

# One mapping, not a list, so `tools_router._rows` does not slice the inner
# lists into rows of their own and lose which list each came from.
RECALL_RESULT_KEYS: Tuple[str, ...] = (
    "keys",
    "as_of",
    "sightings",
    "verdicts",
    "gaps",
    "dropped",
    "ranking",
)

# On every row, not the envelope: one read returns rows from many
# investigations. `concluded_at` is when the investigation concluded, not when
# the row was written — the Distil polls, so an investigation that ended Monday
# can be written Wednesday carrying Monday's date, inside the freshness
# predicate and absent from the read that ran on Tuesday.
_PROVENANCE: Tuple[str, ...] = (
    "investigation_kind",
    "investigation_id",
    "concluded_at",
)

# One row per entity, investigation and source, so growth tracks hunts and not
# telemetry volume.
SIGHTING_FIELDS: Tuple[str, ...] = _PROVENANCE + (
    "entity_key",
    "source_system",
    "hit_count",
    "attacker_influenceable",
    "window",
)

VERDICT_FIELDS: Tuple[str, ...] = _PROVENANCE + (
    # Stable, because the prose gets re-worded.
    "hypothesis_id",
    "statement",
    "outcome",
    # Reaches no ranking function: confident-sounding model text must not move a
    # priority (ADR 0015). Carried for a human reading the run back.
    "rationale",
    # The entities the hypothesis named, typically one to three — not the 38 to
    # 76 its evidence touched, which stay reachable through Sightings (ADR 0016).
    "subject_entities",
    "attacker_influenceable_only",
    "trust",
    "window",
    "window_source",
    "sources",
)

# `source_tier` carries #728's vocabulary and is not restated. `not_evidence`
# here is a defect rather than a weak row — citing a rule catalogue as
# corroboration is a run treating a lookup as an observation — and is
# representable so that it is visible.
VERDICT_SOURCE_FIELDS: Tuple[str, ...] = ("source_system", "stance", "source_tier")

# No window, which is why a Gap is not a Verdict with an empty outcome.
GAP_FIELDS: Tuple[str, ...] = _PROVENANCE + (
    "hypothesis_id",
    "statement",
    "disposition",
    "reason",
    "subject_entities",
)

WINDOW_FIELDS: Tuple[str, ...] = ("first_seen", "last_seen")

# Per kind, and per reason: the two reasons mean different things to a caller.
# A total is the sum and is not restated.
DROPPED_KINDS: Tuple[str, ...] = ("sightings", "verdicts", "gaps")
DROPPED_REASONS: Tuple[str, ...] = ("per_key_cap", "overall_cap")

# How a queried key is matched against a stored one. The rule is `defang` then
# case-fold, in `services/agent/workflows/hunt/entities.ts`; Python reproduces
# it. The exception is the half that breaks silently: an ARN's resource part and
# an AWS key id are case-significant, so folding them makes two principals one
# key and the join still returns rows, just the wrong ones.
KEY_CASE_SENSITIVE_TYPES: Tuple[str, ...] = ("arn", "aws_key")

# The entity types a key may name. Owned by ENTITY_TYPES in
# services/agent/workflows/hunt/types.ts, which is what the harness extracts and
# therefore what a Sighting can ever hold. A writer that mints outside this list
# -- a Case IOC typed `mutex`, say -- writes a key no reader will ever query,
# which reads as an entity nobody has looked at rather than as a bad write.
ENTITY_KEY_TYPES: Tuple[str, ...] = (
    "ip",
    "domain",
    "host",
    "url",
    "email",
    "hash",
    "arn",
    "aws_key",
    "user",
    "process",
)

# How many subjects one Verdict may name. ADR 0016 puts a Hypothesis at
# "typically one to three" and argues at length against a Verdict naming the
# seventy entities its evidence touched. The extractor bounds itself by working
# on one sentence; a Case's IOC list has no such bound, so the bound is stated
# here. Reached rather than assumed: the writer logs what it dropped, because a
# silently truncated subject list reads as a Verdict that named fewer entities.
VERDICT_SUBJECT_CAP = 12

# Longest prose a Verdict or Gap stores (statement, rationale, reason). The text
# is model- or analyst-written and is replayed into the next hunt's opening turn,
# so it is bounded where it is written as well as where it is read.
EPISODIC_PROSE_CAP = 2000

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")


def bounded_prose(value: object, cap: int = EPISODIC_PROSE_CAP) -> str:
    """Prose as stored: control characters (tab and newline aside) dropped, then capped."""
    return _CONTROL_CHARS.sub("", str(value))[:cap]


# A LIMIT over a partial order lets Postgres return a different set on identical
# data, so ties break on the primary key.
RECALL_ORDER = "concluded_at DESC, id ASC"

# Rows per kind, per key — 20 sightings and 20 verdicts and 20 gaps for one key,
# not 20 between them. Applied as a LATERAL and before the overall cap, which
# one entity present in every hunt would otherwise consume by itself.
RECALL_PER_KEY_CAP = 20

# Rows across all kinds and keys, applied after the per-key cap.
RECALL_OVERALL_CAP = 100

# Copied into the result rather than referenced, because a RunSpec records the
# arch by name and not by version. Selection only: the order the rows were
# *presented* in is a fold, so the harness's ranking may change without
# invalidating a historical Ledger. Named `ranking` because #729 and #732 both
# call it that.
RANKING_KEYS: Tuple[str, ...] = ("order", "per_key_cap", "overall_cap")

UNKNOWN_CALLER = "unknown"


def recall_ranking() -> Dict[str, object]:
    """The selection parameters in force, for the result and the read log."""
    return {
        "order": RECALL_ORDER,
        "per_key_cap": RECALL_PER_KEY_CAP,
        "overall_cap": RECALL_OVERALL_CAP,
    }


def empty_dropped() -> Dict[str, Dict[str, int]]:
    """Nothing dropped: zeros rather than an absent key.

    No field in a recall result is nullable and no list is optional, so an
    entity with no history is zeros and empty lists rather than an error.
    """
    return {kind: {reason: 0 for reason in DROPPED_REASONS} for kind in DROPPED_KINDS}
