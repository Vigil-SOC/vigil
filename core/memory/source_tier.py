"""The source-to-Source-Tier map (#728).

Ranking needs two telemetry sources agreeing to outweigh two feeds agreeing,
and needs some sources not to count as evidence at all. Memory owns this map;
the caller stamps what it returns onto the row at write time rather than
joining at read time, so an integration removed or recategorised later cannot
retroactively change how a past Verdict was corroborated. Nothing calls this
yet — `distil` (#731) is the write path.

Three key spaces reach it, not two. The two that ``investigation_kind``
selects between: a hunt names a telemetry domain, narrowed to the playbook's
``data_domains`` by ``attributeSource``; a Case names the ``data_source`` of
the Findings it groups, which is a vendor pipeline. The third is the **MCP
Server Name** — reference servers and sandboxes, which CONTEXT.md defines as
deliberately distinct from both. It is matched before either vocabulary and
spans both kinds, because a server name means the same thing wherever it
lands.

Both land in memory's own ``source_system`` column, so
``resolve_source_tier`` takes the investigation kind to know which vocabulary
it is reading.
"""

import logging
from enum import Enum
from typing import Dict, Set

from core.integrations._base.descriptor import iter_descriptors

logger = logging.getLogger(__name__)


class SourceTier(str, Enum):
    """What a source *is*, as distinct from who concluded (**Trust**)."""

    TELEMETRY = "telemetry"
    FEED = "feed"
    NOT_EVIDENCE = "not_evidence"


class InvestigationKind(str, Enum):
    """Which vocabulary a source name is drawn from."""

    HUNT = "hunt"
    CASE = "case"


DEFAULT_TIER = SourceTier.FEED

# Reference and harness-internal names. A rulebook cited as corroboration is a
# run treating a lookup as an observation; the critic arguing with itself, an
# operator declaring a blind spot and the dispatcher recording a failed tool
# call are none of them a second look at the estate. `not_evidence` on a Verdict
# is a defect rather than a weak row, so these exist to make it visible.
#
# The harness-internal names reach the ledger under their own names because
# `attributeSource` collapses only `provenance === "worker"`, and each is
# appended under a different provenance. There are four such categories, not
# three: `critic` (null_check), `operator` (operator_gap), `dispatcher`, and
# enrichment, which sets `source_system` to an unbounded `chain.id` and so
# cannot be enumerated here — it takes the default.
#
# security-detections cannot arrive on either path today: it writes no
# findings, and a worker naming it collapses to `undeclared` first. It is
# listed because AC 4 asks for the guard, and a guard is meant to have no hits
# in healthy data.
_NOT_EVIDENCE: Set[str] = {
    "security-detections",
    "critic",
    "operator",
    "dispatcher",
}

# The nine values present in `findings.data_source`, enumerated against live
# data on 2026-08-28: `loglm` carries 98% of rows and the rest are the ingest
# and demo pipeline labels behind it. All nine observe our own estate.
#
# A census of what the column holds, not of what it can hold: `data_source` is
# caller-supplied (`ingestion_service` takes it as a parameter), the Kafka
# consumer mints `kafka:<topic>`, and a connector extension brands findings
# with its own manifest id, so vendor names are a designed path. Values outside
# this set are expected and take the default.
_CASE_TIERS: Dict[str, SourceTier] = {
    "loglm": SourceTier.TELEMETRY,
    "firewall": SourceTier.TELEMETRY,
    "proxy": SourceTier.TELEMETRY,
    "flow": SourceTier.TELEMETRY,
    "edr": SourceTier.TELEMETRY,
    "dns": SourceTier.TELEMETRY,
    "email": SourceTier.TELEMETRY,
    "endpoint": SourceTier.TELEMETRY,
    "siem": SourceTier.TELEMETRY,
}

# Two sets of `data_domains`. `network`/`authentication`/`endpoint` are the
# shipped threat-hunt WORKFLOW.md's; `cloud_audit`/`identity`/`endpoint_process`
# appear in no repo file but are what every live hunt declared as of
# 2026-08-28, so a spec is reaching the runtime from outside the tree. A worker
# label outside its own playbook's list never arrives here under that name —
# `attributeSource` collapses it to `undeclared` first.
_HUNT_TIERS: Dict[str, SourceTier] = {
    "network": SourceTier.TELEMETRY,
    "authentication": SourceTier.TELEMETRY,
    "endpoint": SourceTier.TELEMETRY,
    "cloud_audit": SourceTier.TELEMETRY,
    "identity": SourceTier.TELEMETRY,
    "endpoint_process": SourceTier.TELEMETRY,
    # The collapse already decided this source could not be vouched for, so it
    # takes the default rather than the tier of the domains around it.
    "undeclared": SourceTier.FEED,
}

# Known pipelines whose suffix is caller-chosen, so they cannot be enumerated
# as exact keys. Matched by prefix purely to keep a recognised pipeline out of
# the unknown-source log; the tier is the default either way, and reading a
# stronger tier off a prefix anyone can mint is what this deliberately avoids.
# Known names that take :data:`DEFAULT_TIER` deliberately rather than by
# failing to match. One shape for all of them, so "we decided this is a feed"
# never reads as "we forgot this one": the tier is the default either way, and
# the only behavioural difference is that these do not log.
#
# Detonation is a real observation, but of an artifact rather than of our
# estate, so two sandboxes agreeing is not two independent looks at it. The
# ingest fallbacks stand in when a row declared no source of its own, which is
# exactly the unstated provenance the conservative direction is for.
_KNOWN_DEFAULTED: Set[str] = {
    "cape-sandbox",
    "joe-sandbox",
    "imported",
    "csv_import",
}

# The same idea where the name's suffix is caller-chosen and so cannot be
# enumerated. Reading a stronger tier off a prefix anyone can mint is what this
# deliberately avoids; matching at all just keeps a recognised pipeline out of
# the unknown-source log.
_KNOWN_DEFAULTED_PREFIXES: Set[str] = {
    "kafka:",
}

# Vigil's own servers and the Catalog Entries. Neither can declare a category
# of its own: the first are not products anyone sells, and the second are
# integrations Vigil holds a credential for but carries no code for, which per
# CONTEXT.md is exactly what has no Integration Descriptor. Everything else
# comes from `_INTEGRATION_TIERS` below rather than being listed here.
#
# They do not share a tier. A search of someone's logs is an observation;
# reading our own findings back is a run agreeing with itself.
_UNDECLARED_SERVERS: Dict[str, SourceTier] = {
    "vigil": SourceTier.NOT_EVIDENCE,
    # The name Vigil's own server carried before it was one server. Existing
    # `source_system` rows cite it, so it is graded rather than migrated away.
    "deeptempo-findings": SourceTier.NOT_EVIDENCE,
    "approval": SourceTier.NOT_EVIDENCE,
    "gcp-secops": SourceTier.TELEMETRY,
    "gcp-scc": SourceTier.TELEMETRY,
    "cribl-stream": SourceTier.TELEMETRY,
    "gcp-threat-intel": SourceTier.FEED,
    "github": SourceTier.NOT_EVIDENCE,
}

# What a whole category of product is, so a new integration is graded on
# arrival instead of defaulting to `feed` until someone edits this module.
# `category` is free text and inconsistent with it — `EDR` and `EDR/XDR` are
# both in use — so variants are listed rather than normalised away.
#
# `Communications` is a feed, not `not_evidence`: an analyst asserting
# something in Slack is a claim about the world, and `not_evidence` is reserved
# for things that assert nothing at all.
_CATEGORY_TIERS: Dict[str, SourceTier] = {
    "EDR": SourceTier.TELEMETRY,
    "EDR/XDR": SourceTier.TELEMETRY,
    "SIEM": SourceTier.TELEMETRY,
    "Cloud Security": SourceTier.TELEMETRY,
    "Identity & Access": SourceTier.TELEMETRY,
    "Network Security": SourceTier.TELEMETRY,
    "Threat Intelligence": SourceTier.FEED,
    "Sandbox Analysis": SourceTier.FEED,
    "Communications": SourceTier.FEED,
    "Incident Management": SourceTier.NOT_EVIDENCE,
}


def _integration_tiers() -> Dict[str, SourceTier]:
    """Grade every integration from the category it declares about itself.

    Built once at import. A vendor already states what kind of product it is,
    so the alternative — transcribing thirty-odd server names here — would go
    stale the first time someone added one.
    """
    tiers: Dict[str, SourceTier] = {}
    for descriptor in iter_descriptors():
        tier = _CATEGORY_TIERS.get(descriptor.category)
        if tier is None:
            logger.warning(
                "Source Tier: integration %r declares category %r, which maps "
                "to no tier; its sources will take the default",
                descriptor.id,
                descriptor.category,
            )
            continue
        for name in descriptor.mcp_server_names:
            tiers[name] = tier
    return tiers


_INTEGRATION_TIERS: Dict[str, SourceTier] = _integration_tiers()

_TIERS_BY_KIND: Dict[InvestigationKind, Dict[str, SourceTier]] = {
    InvestigationKind.CASE: _CASE_TIERS,
    InvestigationKind.HUNT: _HUNT_TIERS,
}


def resolve_source_tier(
    source_system: str, investigation_kind: InvestigationKind
) -> SourceTier:
    """Return the **Source Tier** of ``source_system`` in its own vocabulary.

    An unknown source resolves to :data:`DEFAULT_TIER` and is logged rather
    than raising: a source memory has never seen must not stop an
    investigation being distilled, and grading it a feed understates its
    corroborating weight instead of overstating it.

    This makes a `not_evidence` source visible, not impossible. User story 38
    wants it rejected outright on a Verdict, which is a write-path decision and
    belongs to `distil` (#731); a map that raised here would fail the whole
    investigation over one bad citation.
    """
    name = (source_system or "").strip().lower()
    if not name:
        logger.warning(
            "Source Tier: empty source_system on a %s, defaulting to %s",
            investigation_kind.value,
            DEFAULT_TIER.value,
        )
        return DEFAULT_TIER

    if name in _NOT_EVIDENCE:
        return SourceTier.NOT_EVIDENCE

    server_tier = _UNDECLARED_SERVERS.get(name) or _INTEGRATION_TIERS.get(name)
    if server_tier is not None:
        return server_tier

    if name in _KNOWN_DEFAULTED or any(
        name.startswith(prefix) for prefix in _KNOWN_DEFAULTED_PREFIXES
    ):
        return DEFAULT_TIER

    tier = _TIERS_BY_KIND[investigation_kind].get(name)
    if tier is None:
        logger.warning(
            "Source Tier: unknown source %r on a %s, defaulting to %s",
            source_system,
            investigation_kind.value,
            DEFAULT_TIER.value,
        )
        return DEFAULT_TIER

    return tier
