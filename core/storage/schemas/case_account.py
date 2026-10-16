"""The case account: what a run concluded, in the shape every loop fills.

Mirrors ``services/agent/contracts/account.ts``, which says what each field
means; the comments here say only what differs on this side. The fixtures
under ``services/agent/tests/fixtures/account`` hold the two to the same JSON.

An absent section means the loop does not produce it, so nothing in an account
is null: a value a run does not have is left out. Validation refuses a null
rather than reading it as absent, as the TypeScript type does, and
:func:`dump_account` leaves out whatever was never set.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator
from pydantic.json_schema import SkipJsonSchema

ACCOUNT_SCHEMA_VERSION = 1

RunKind = Literal[
    "hunt",
    "root_cause",
    "adjudicate",
    "investigate",
    "compose",
    "chat",
    "tally",
    "subagent",
]
Disposition = Literal[
    "malicious",
    "suspicious",
    "benign",
    "false_positive",
    "inconclusive",
    "needs_review",
]
Confidence = Literal["low", "medium", "high"]
ExplanationRole = Literal["adversary", "benign"]
Standing = Literal[
    "proven", "supported", "open", "inconclusive", "weakened", "ruled_out", "set_aside"
]
Stance = Literal["supports", "weakens", "neither"]
CheckStatus = Literal["match", "differs", "absent"]
Strength = Literal["direct", "corroborating", "circumstantial"]
Salience = Literal["routine", "notable", "anomalous"]
ChainStatus = Literal["confirmed", "suspected", "unproven", "rejected"]
AccountRole = Literal["attacker", "victim", "legitimate"]
GapKind = Literal["visibility", "open_question", "disputed"]

# The entity types a key may name: ENTITY_TYPES in
# services/agent/contracts/account.ts, which the hunt extractor's list in
# plugins/hunt/types.ts matches (a ratchet compares each with this tuple).
# Owned here, below memory, which takes its ENTITY_KEY_TYPES from this tuple.
ENTITY_TYPES: tuple[str, ...] = (
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

# A value is required after the colon. The TypeScript template cannot say so; the
# extractor, which mints every key, is the guard there.
EntityKey = Annotated[
    str, StringConstraints(pattern=rf"^({'|'.join(ENTITY_TYPES)}):.+")
]

# Optional without null: accepted when left out, and kept out of the schema as a
# value, so the generated frontend type reads ``field?: T`` like the contract.
Absent = SkipJsonSchema[None]


# Strict, because the TypeScript type coerces nothing: "3" is not a turn there and
# must not become one here. The one gap left is that a turn or a count is an int on
# this side and a number on that one, so 3.5 passes there and is refused here.
class _AccountModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def _absent_not_null(cls, data: Any) -> Any:
        # ``Any`` fields are the checked values themselves, where null is a value.
        if isinstance(data, dict):
            nulls = sorted(
                key
                for key, value in data.items()
                if value is None
                and key in cls.model_fields
                and cls.model_fields[key].annotation is not Any
            )
            if nulls:
                raise ValueError(
                    f"an account leaves out what it lacks, never null: {nulls}"
                )
        return data


class AccountRun(_AccountModel):
    run_id: str
    run_kind: RunKind


class Verdict(_AccountModel):
    run_id: str
    disposition: Disposition
    confidence: Confidence | Absent = None
    headline: str
    basis: str | Absent = None


class Narrative(_AccountModel):
    run_id: str
    summary: str
    what_happened: str | Absent = None
    written_by: Literal["vigil"] | Absent = None
    missing_because: str | Absent = None
    model_id: str | Absent = None


class Explanation(_AccountModel):
    id: str
    run_id: str
    claim: str
    role: ExplanationRole
    standing: Standing
    reason: str | Absent = None
    techniques: list[str] | Absent = None
    subjects: list[EntityKey] | Absent = None


class Bearing(_AccountModel):
    on: str
    stance: Stance


class ValueCheck(_AccountModel):
    field: str
    claimed: Any
    status: CheckStatus
    ref: str | Absent = None
    found_field: str | Absent = None
    found: Any = None
    rounded: Literal[True] | Absent = None


class EvidenceItem(_AccountModel):
    id: str
    run_id: str
    claim: str
    why: str | Absent = None
    source: str | Absent = None
    turn: int | Absent = None
    salience: Salience | Absent = None
    strength: Strength | Absent = None
    absence: Literal[True] | Absent = None
    attested: bool | Absent = None
    source_ref: str | Absent = None
    source_refs: list[str] | Absent = None
    checks: list[ValueCheck] | Absent = None
    bears_on: list[Bearing] | Absent = None
    techniques: list[str] | Absent = None
    entities: list[EntityKey] | Absent = None


class ChainTie(_AccountModel):
    id: str | Absent = None
    text: str | Absent = None
    status: ChainStatus
    confidence: float | Absent = None


class ChainEvent(_AccountModel):
    id: str
    run_id: str
    text: str
    at: str | Absent = None
    source: str | Absent = None
    status: ChainStatus
    confidence: float | Absent = None
    causes: str | Absent = None
    tie: ChainTie | Absent = None
    together_with: list[str] | Absent = None
    origin: ChainStatus | Absent = None
    why_origin: str | Absent = None
    ran_as: str | Absent = None
    operated_by: str | Absent = None
    entities: list[EntityKey] | Absent = None


class ChainPath(_AccountModel):
    path: list[str]
    score: float | Absent = None


class Chain(_AccountModel):
    run_id: str
    events: list[ChainEvent]
    score: float | Absent = None
    alternatives: list[ChainPath] | Absent = None


class TimelineEntry(_AccountModel):
    run_id: str
    text: str
    at: str | Absent = None
    stage: str | Absent = None
    techniques: list[str] | Absent = None
    ref: str | Absent = None
    source: str | Absent = None
    status: ChainStatus | Absent = None
    confidence: float | Absent = None
    on_chain: bool | Absent = None
    legitimate: Literal[True] | Absent = None
    ran_as: str | Absent = None
    operated_by: str | Absent = None


class Account(_AccountModel):
    run_id: str
    name: str
    role: AccountRole
    note: str | Absent = None
    seen_as: list[str] | Absent = None
    entities: list[EntityKey] | Absent = None


class EntityItem(_AccountModel):
    key: EntityKey
    run_id: str
    indicator: Literal[True] | Absent = None


class NextStep(_AccountModel):
    run_id: str
    text: str


class Gap(_AccountModel):
    run_id: str
    text: str
    kind: GapKind | Absent = None
    on: str | Absent = None


class Escalation(_AccountModel):
    run_id: str
    case_id: str
    rationale: str
    explanation_id: str | Absent = None
    at: str | Absent = None


class Step(_AccountModel):
    run_id: str
    n: int
    kind: str
    text: str
    at: str | Absent = None
    cost_usd: float | Absent = None
    duration_ms: int | Absent = None
    refs: list[str] | Absent = None


class CaseAccount(_AccountModel):
    schema_version: int
    runs: list[AccountRun]
    verdict: Verdict | Absent = None
    narrative: Narrative | Absent = None
    explanations: list[Explanation] | Absent = None
    evidence: list[EvidenceItem] | Absent = None
    chain: Chain | Absent = None
    timeline: list[TimelineEntry] | Absent = None
    accounts: list[Account] | Absent = None
    entities: list[EntityItem] | Absent = None
    next_steps: list[NextStep] | Absent = None
    gaps: list[Gap] | Absent = None
    escalations: list[Escalation] | Absent = None
    steps: list[Step] | Absent = None


def dump_account(account: CaseAccount) -> dict[str, Any]:
    """The account as JSON, with every section the loop did not fill left out.

    By what was set rather than by value, so a checked value that is null stays.
    """
    return account.model_dump(mode="json", exclude_unset=True)
