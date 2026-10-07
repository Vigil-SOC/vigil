import { fromText } from "./entities.js";
import { fold, type HuntEvent, type Projection } from "./ledger.js";
import { evidenceStrength, isGap, NULL_CHECK_PROVENANCE } from "./strength.js";
import type { Entity, EvidenceRecord, HuntOutcome, HypothesisStatus, LinkRelation } from "./types.js";

// What the Distil (#731) is told about a finished hunt, folded here because the
// fold is this side's. The projection cannot serve it: a supervisor is told how
// each belief stands and how many records were gathered, never which entities
// they touched or which system produced them, and Sightings are exactly those.
// Re-folding the ledger in Python would be the second implementation that drifts
// while only one is gated, so the fields are asked for here instead.
//
// Facts, not rows. The status-to-row mapping, the Entity Keys and the Source
// Tiers are the Python domain's; this bumps when what this side *can* say changes.
export const DISTIL_SCHEMA_VERSION = 2;

// A record the hunt gathered, as opposed to one it wrote to itself. The three
// excluded provenances are the harness talking: `dispatcher` when a query could
// not run, `operator` when a human declared a blind spot, `critic` when it argued
// the benign case. Python's tier map independently grades all three
// `not_evidence`, so a record that slips this filter is visible rather than
// silently counted as an observation.
function observed(record: EvidenceRecord): boolean {
  return !isGap(record) && record.provenance !== NULL_CHECK_PROVENANCE;
}

function dedupe(entities: readonly Entity[]): Entity[] {
  const seen = new Map<string, Entity>();
  for (const entity of entities) seen.set(`${entity.type}:${entity.value}`, entity);
  return [...seen.values()];
}

// Candidate data, deliberately. Only Python writes an Entity Key, so the type and
// the value travel separately and this extractor's output is never a stored key.
export interface DistilSighting {
  entity: Entity;
  source_system: string;
  hit_count: number;
  // Aggregated over the group: one record an adversary could have authored is
  // enough to say so of the group, which is the fail-closed direction.
  attacker_influenceable: boolean;
  first_seen: string;
  last_seen: string;
}

export interface DistilSource {
  source_system: string;
  // `weakens` beats `supports` beats `neither` where one system did both. A
  // source that ever argued against the claim is not a clean corroborator, and
  // collapsing the other way would let a corroboration count include it.
  stance: LinkRelation;
}

// One per Hypothesis, before the status map decides whether it becomes a Verdict
// or a Declared Gap. That decision needs the schema and belongs beside it.
export interface DistilConclusion {
  hypothesis_id: string;
  statement: string;
  status: HypothesisStatus;
  // The harness's own words for why it landed here. Reaches no ranking function
  // (ADR 0015); carried for a human reading the run back.
  rationale: string;
  // What the claim is about, not what it touched: a hypothesis touches forty to
  // seventy entities and is about one to three of them (ADR 0016). Declared by the
  // caller who put the claim up; the statement is read only when they declared
  // nothing, and it answers for the seven types that have a free-text shape.
  subject_entities: Entity[];
  // Linked observations. The status map reads it as "was evidence gathered",
  // which separates an inconclusive that looked from one that never did.
  evidence_count: number;
  attacker_influenceable_only: boolean;
  sources: DistilSource[];
  // Distinct ATT&CK ids the gathered evidence bearing on this claim cited, not
  // what the playbook declared it would test. Empty is known-to-be-none, and is
  // what lets a later coverage check ask "have we hunted T1071.001?".
  techniques: string[];
  first_seen: string;
  last_seen: string;
  // False when the window fell back to the hunt's own dates because nothing was
  // linked. Ranking discounts an asserted window against an observed one.
  window_observed: boolean;
}

export interface DistilPayload {
  investigation_kind: "hunt";
  // The hunt, not the run. A run-keyed schema cannot represent the Case-authored
  // Verdicts that follow, and a resumed hunt is one investigation across two runs.
  investigation_id: string;
  // The hunt's own outcome, which the terminal event does not carry: `outcomeOf`
  // narrows it to the domain-free RunOutcome set on the way out, so `aborted`,
  // `budget_terminated` and `data_starved` are distinguishable only here.
  outcome: HuntOutcome | null;
  // When the hunt ended. Empty while it has not, which the Distil refuses.
  concluded_at: string;
  distil_schema_version: number;
  sightings: DistilSighting[];
  conclusions: DistilConclusion[];
}

interface Group {
  entity: Entity;
  source_system: string;
  hit_count: number;
  attacker_influenceable: boolean;
  first_seen: string;
  last_seen: string;
}

// One row per entity, investigation and source, so growth tracks hunts and not
// telemetry volume. A record naming the same entity twice counts once: the hit
// count means records, and the extractor already deduped within one of them.
function sightingsIn(projection: Projection): DistilSighting[] {
  const groups = new Map<string, Group>();

  for (const record of projection.evidence.values()) {
    if (!observed(record)) continue;
    for (const entity of dedupe(record.entities)) {
      const id = `${entity.type}:${entity.value} ${record.source_system}`;
      const group = groups.get(id);
      if (group === undefined) {
        groups.set(id, {
          entity,
          source_system: record.source_system,
          hit_count: 1,
          attacker_influenceable: record.attacker_influenceable,
          first_seen: record.captured_at,
          last_seen: record.captured_at,
        });
        continue;
      }
      group.hit_count += 1;
      group.attacker_influenceable ||= record.attacker_influenceable;
      if (record.captured_at < group.first_seen) group.first_seen = record.captured_at;
      if (record.captured_at > group.last_seen) group.last_seen = record.captured_at;
    }
  }

  return [...groups.values()];
}

const PRECEDENCE: Record<LinkRelation, number> = { neither: 0, supports: 1, weakens: 2 };

function conclusionsIn(projection: Projection): DistilConclusion[] {
  const hunt = projection.hunt;

  return [...projection.hypotheses.values()].map((hypothesis) => {
    const linked = projection.links
      .filter((link) => link.hypothesis_id === hypothesis.hypothesis_id)
      .flatMap((link) => {
        const record = projection.evidence.get(link.evidence_id);
        return record === undefined ? [] : [{ relation: link.relation, record }];
      });

    // Over what was gathered, and nothing else. A dispatcher's failed query, an
    // operator's declared blind spot and the critic's benign case are the harness
    // talking, and citing one as corroboration is the rulebook-as-observation
    // defect the Source Tier map exists to catch. Filtered here rather than
    // graded `not_evidence` downstream, so it cannot reach a Verdict at all.
    const gathered = linked.filter(({ record }) => observed(record));

    const stances = new Map<string, LinkRelation>();
    for (const { relation, record } of gathered) {
      const held = stances.get(record.source_system);
      if (held === undefined || PRECEDENCE[relation] > PRECEDENCE[held]) {
        stances.set(record.source_system, relation);
      }
    }

    // Same rule as `citedTechniques`, but over `gathered` rather than every link:
    // a failed dispatch or the critic's benign case naming a technique must not
    // put that technique on a Verdict.
    const techniques = new Set<string>();
    for (const { relation, record } of gathered) {
      if (relation !== "neither" && record.attack_technique) techniques.add(record.attack_technique);
    }

    // A gap record's capture time never stands in for having seen something.
    const times = gathered.map(({ record }) => record.captured_at).sort();
    const first = times[0];
    const last = times[times.length - 1];
    return {
      hypothesis_id: hypothesis.hypothesis_id,
      statement: hypothesis.statement,
      status: hypothesis.status,
      rationale: hypothesis.resolution_reason ?? "",
      subject_entities: dedupe(hypothesis.subjects ?? fromText(hypothesis.statement)),
      evidence_count: times.length,
      attacker_influenceable_only: evidenceStrength(projection, hypothesis.hypothesis_id).attacker_influenceable_only,
      sources: [...stances].map(([source_system, stance]) => ({ source_system, stance })),
      techniques: [...techniques].sort(),
      // A hypothesis that concluded on nothing gathered still has a window, and
      // saying which dates those are is the honest version of leaving it null.
      first_seen: first ?? hunt.created_at,
      last_seen: last ?? hunt.terminated_at ?? hunt.created_at,
      window_observed: first !== undefined,
    };
  });
}

export function huntDistil(_runId: string, events: readonly HuntEvent[]): DistilPayload {
  const projection = fold(events);

  return {
    investigation_kind: "hunt",
    investigation_id: projection.hunt.hunt_id,
    outcome: projection.hunt.outcome,
    concluded_at: projection.hunt.terminated_at ?? "",
    distil_schema_version: DISTIL_SCHEMA_VERSION,
    sightings: sightingsIn(projection),
    conclusions: conclusionsIn(projection),
  };
}
