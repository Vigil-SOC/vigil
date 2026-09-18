"""Episodic memory ORM models (#731).

What investigations saw and what they concluded, joined on entity keys.
Everything is keyed by investigation_kind + investigation_id and never by
run_id: a run-keyed schema cannot represent the Case-authored Verdicts that
follow, which have a Case behind them and no run at all.

No column is nullable. An empty list means known-to-be-none, and there is no
unknown state to represent.

The domains these columns range over are stated once, in
infra/database/init/26_episodic_memory.sql, as every other table in that
directory states them. They are not restated here: `core/memory` owns the
vocabularies and `core/storage` is the tier underneath it, so mirroring them
would mean the shared-infrastructure tier importing a capability domain
(CONTEXT.md, and the `tiers` contract in .importlinter). These models carry
the uniqueness and the indexes, which is what the rest of this package carries.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base


class EpisodicSighting(Base):
    """What an investigation observed: one row per entity, investigation and source.

    Never one row per evidence record — growth tracks investigations, not
    telemetry volume, so a hunt that saw one address ten thousand times in one
    system writes one row carrying a hit count.
    """

    __tablename__ = "episodic_sightings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # ``type:value``, normalised by Python. Only Python writes an Entity Key;
    # the harness's extractor output arrives as a candidate type and value.
    entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    investigation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    investigation_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Memory's own column, not a foreign key into either producer: a hunt fills
    # it from the Ledger's source_system, a Case from its Findings' data_source.
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    attacker_influenceable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the investigation concluded, not when the row was written: the Distil
    # polls, so one that ended Monday can be written Wednesday carrying Monday.
    concluded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "entity_key",
            "investigation_kind",
            "investigation_id",
            "source_system",
            name="episodic_sightings_unique",
        ),
        # The recall join and the order it reads in. Ties break on the primary
        # key: a LIMIT over a partial order lets Postgres return a different set
        # on identical data, which surfaces as a replay diff rather than an error.
        Index(
            "idx_episodic_sightings_recall",
            "entity_key",
            text("concluded_at DESC"),
            "id",
        ),
        Index(
            "idx_episodic_sightings_investigation",
            "investigation_kind",
            "investigation_id",
        ),
    )


class EpisodicVerdict(Base):
    """What an investigation concluded, one row per Hypothesis."""

    __tablename__ = "episodic_verdicts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    investigation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    investigation_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Stable, because the prose gets re-worded. A Case's is its case id.
    hypothesis_id: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    # Reaches no ranking function: confident-sounding model text must not move a
    # priority (ADR 0015). Carried for a human reading the run back.
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    # The entities the Hypothesis named, typically one to three — not the 38 to
    # 76 its evidence touched, which stay reachable through Sightings (ADR 0016).
    # Empty is legitimate: "is there any lateral movement at all" names none.
    subject_entities: Mapped[List[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")
    )
    # ATT&CK ids the gathered evidence cited, so a coverage check can ask which
    # hunts bore on a technique. Empty is known-to-be-none; a Case writes none.
    techniques: Mapped[List[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")
    )
    attacker_influenceable_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Who concluded, as distinct from what the source is.
    trust: Mapped[str] = mapped_column(String(16), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # A retrospective sweep over old archives asserts its window rather than
    # observing it, and ranking discounts the weaker one.
    window_source: Mapped[str] = mapped_column(String(16), nullable=False)
    concluded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "investigation_kind",
            "investigation_id",
            "hypothesis_id",
            name="episodic_verdicts_unique",
        ),
        # Recall joins Verdicts on the subject array rather than a key column.
        Index(
            "idx_episodic_verdicts_subjects",
            "subject_entities",
            postgresql_using="gin",
        ),
        Index(
            "idx_episodic_verdicts_techniques",
            "techniques",
            postgresql_using="gin",
        ),
        Index("idx_episodic_verdicts_recall", text("concluded_at DESC"), "id"),
        Index(
            "idx_episodic_verdicts_investigation",
            "investigation_kind",
            "investigation_id",
        ),
    )


class EpisodicVerdictSource(Base):
    """One row per Verdict and source, carrying direction.

    Replaces a flat corroborated list, which cannot say that a source argued
    against the claim.
    """

    __tablename__ = "episodic_verdict_sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    verdict_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("episodic_verdicts.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    # Stamped at write time and never joined at read time: an integration removed
    # or recategorised later must not retroactively change how a past Verdict was
    # corroborated. ``not_evidence`` here is a defect rather than a weak row, and
    # is representable so that it is visible.
    source_tier: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "verdict_id", "source_system", name="episodic_verdict_sources_unique"
        ),
    )


class EpisodicGap(Base):
    """A question an investigation never gathered evidence for.

    No activity window, which is the reason these are not Verdict rows carrying
    an empty outcome.
    """

    __tablename__ = "episodic_gaps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    investigation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    investigation_id: Mapped[str] = mapped_column(Text, nullable=False)
    hypothesis_id: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    subject_entities: Mapped[List[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")
    )
    concluded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "investigation_kind",
            "investigation_id",
            "hypothesis_id",
            name="episodic_gaps_unique",
        ),
        Index("idx_episodic_gaps_subjects", "subject_entities", postgresql_using="gin"),
        Index("idx_episodic_gaps_recall", text("concluded_at DESC"), "id"),
        Index(
            "idx_episodic_gaps_investigation", "investigation_kind", "investigation_id"
        ),
    )


class EpisodicDistilMarker(Base):
    """The only record that an investigation was processed.

    Deliberately not derived from the presence of rows: an investigation that
    concluded nothing writes no Sightings, no Verdicts and no Gaps, and is still
    done. Written in the same transaction as the rows.
    """

    __tablename__ = "episodic_distil_markers"

    investigation_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # The terminal these rows were derived from, so a marker can be traced back
    # to a ledger. Null on a Case, which was closed rather than run — the only
    # nullable pair in this schema, and not an unknown state: which of the two
    # it is follows from investigation_kind, and the DDL's CHECK says so.
    origin_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # The seq of that terminal. A hunt that resumed past its own terminal appends
    # a second one to the same run, and comparing seq is what makes the later
    # conclusions re-derive instead of being skipped as a run already seen.
    origin_seq: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Every run this investigation has been distilled from, origin_run_id
    # included. One investigation can span more than one run, and the poll has
    # only run ids to work with because agent_events carries no investigation id;
    # without this, each run of one investigation misses the other's marker and
    # both re-distil on every tick forever.
    origin_run_ids: Mapped[List[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        server_default=text("ARRAY[]::uuid[]"),
    )
    # Bumped when the mapping changes. Re-deriving is delete-then-insert, so a
    # bump that now yields fewer rows leaves none of the old ones behind.
    distil_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Set, never incremented: a count that drifts from its rows is worse than no
    # count, because it reads as authoritative.
    sightings_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verdicts_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gaps_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    concluded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    distilled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("idx_episodic_markers_origin", "origin_run_ids", postgresql_using="gin"),
    )


class EpisodicDistilFailure(Base):
    """One subject a Distil could not write, and when it may be tried again.

    Why it is keyed by run and case rather than by investigation is stated once,
    in ``infra/database/init/29_episodic_distil_failures.sql``; what the retry
    schedule is and why it paces rather than gives up belongs to the code that
    decides it, ``core/memory/distil.py``. A successful write deletes the row in
    the transaction that writes the marker.
    """

    __tablename__ = "episodic_distil_failures"

    investigation_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    # A run id for a hunt, a case id for a Case: what the poll holds before it
    # has an investigation id, which the two commonest failures never reach.
    failure_key: Mapped[str] = mapped_column(Text, primary_key=True)
    origin_seq: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, nullable=False)
    first_failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    distil_version: Mapped[int] = mapped_column(Integer, nullable=False)


class EpisodicReadLog(Base):
    """One row per read of episodic memory, for audit rather than replay.

    Why it exists and why it alone is retained is stated once, in
    ``infra/database/init/27_episodic_read_log.sql``.
    """

    __tablename__ = "episodic_read_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    caller_kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unknown'")
    )
    caller_id: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unknown'")
    )
    keys: Mapped[List[str]] = mapped_column(ARRAY(Text), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_counts: Mapped[dict] = mapped_column(JSONB, nullable=False)
    dropped: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ranking: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index("idx_episodic_read_log_ts", "ts"),
        Index("idx_episodic_read_log_keys", "keys", postgresql_using="gin"),
    )
