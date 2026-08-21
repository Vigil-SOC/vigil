import { openCheckpoint, type OpenCheckpoint } from "../../contracts/events.js";
import { fold, type HuntEvent, type Projection } from "./ledger.js";
import { citedTechniques, isGap, sensorAttested } from "./strength.js";
import { renderReport, type HuntReport } from "./report.js";
import { stepsOf, type Narrative } from "./narrative.js";
import type {
  Budgets,
  DecisionRecord,
  EvidenceRecord,
  Handoff,
  HuntOutcome,
  HuntState,
  HuntStatus,
  Hypothesis,
  HypothesisStatus,
  LinkRelation,
  RestsOn,
  Salience,
} from "./types.js";

// What a reader outside this process is told about a hunt. A hunt has no steps to
// report progress against, so what it has tested and how each belief stands is it.
export interface HuntProjection {
  run_id: string;
  status: HuntStatus;
  outcome: HuntOutcome | null;
  reason: string;
  iteration: number;
  cost_usd: number;
  // What this run was granted, extensions included, so a reader can say how far
  // through its budget the hunt is.
  budgets: Budgets;
  hypotheses: HypothesisStanding[];
  evidence_count: number;
  // The records themselves, newest first, so a reader need not wait for the report.
  // Capped; evidence_count above stays the untruncated total.
  evidence: EvidenceView[];
  open_checkpoint: OpenCheckpoint | null;
  // The deliverable, null until the hunt writes one. Rendered here because the
  // renderer is this side's: a reader that formatted the report itself would be a
  // second opinion about what a hunt found.
  report: HuntReport | null;
  report_markdown: string | null;
  // The account as data rather than baked into the markdown, so a console can lay
  // it out rather than re-render a wall of text.
  narrative: HuntNarrative | null;
  // What the hunt asked someone else to take on, each carrying its own case file.
  handoffs: Handoff[];
  // The frontier: leads opened and not yet taken, in the order a worker would take
  // them. An operator who cannot see these cannot pin one with a boost directive.
  open_questions: QuestionView[];
  // Every move the Hunt Lead made and why, newest first: the standings say what a
  // hunt believes, never how it got there.
  moves: MoveView[];
}

// next_steps normalised to strings here, so every reader downstream gets one shape:
// the ledger holds both, the schema having asked for objects before strings.
export interface HuntNarrative {
  summary: string;
  what_happened: string;
  next_steps: string[];
  model_id: string;
  written_at: string;
}

export interface QuestionView {
  question_id: string;
  question: string;
  entity_key: string | null;
  hypothesis_id: string | null;
  spawned_iteration: number;
}

// One decision, without the digest it was made over: that payload is a whole prompt,
// and this is read on a five-second poll.
export interface MoveView {
  decision_id: string;
  iteration: number;
  action: string;
  rationale: string;
  target_entity: string | null;
  target_hypothesis_id: string | null;
  query_intent: string;
  worker_agent_id: string | null;
  evidence_citations: string[];
  cost_usd: number;
  // What was refused before this move was accepted: a stalled turn is nothing but these.
  rejected_attempts: string[];
  created_at: string;
}

// What a piece of evidence is to somebody watching. The payload is left out: it is the
// worker's raw answer, sized for a model rather than a table.
export interface EvidenceView {
  evidence_id: string;
  iteration: number;
  source_system: string;
  summary: string;
  why_notable: string;
  salience: Salience;
  attack_technique: string | null;
  // A record an adversary could have written cannot carry a verdict alone.
  attacker_influenceable: boolean;
  // Whether anything this finding rests on was attested by the telemetry. Computed
  // here rather than in the console, so a reader sees the rule a verdict is gated on
  // rather than a second opinion about it.
  sensor_attested: boolean;
  // Which values, and who chose each. Empty on a run written before the split.
  rests_on: RestsOn[];
  instruction_like: boolean;
  provenance: string;
  // A blind spot rather than a finding: "not there" and "could not look" otherwise read
  // identically.
  is_gap: boolean;
  // Why the hunt could not look, for the operator only: off the summary, because a
  // transport error names our plumbing rather than the estate.
  gap_detail: string | null;
  captured_at: string;
  // Which beliefs it bears on and how. Evidence attached to nothing is worth seeing.
  bears_on: { hypothesis_id: string; relation: LinkRelation }[];
}

export interface HypothesisStanding {
  hypothesis_id: string;
  statement: string;
  status: HypothesisStatus;
  // What a belief was declared to test, which nothing declares any more. Kept because
  // the ledger's record carries it and a historical run has one.
  attack_technique: string | null;
  // What evidence bearing on this belief actually cited: earned rather than asserted.
  techniques_cited: string[];
  resolution_reason: string | null;
  // Where the belief came from: the definition, the caller, or the base rate.
  provenance: string;
}

export function huntProjection(runId: string, events: readonly HuntEvent[]): HuntProjection {
  const view = fold(events);
  const answered = new Set(view.resolutions.map((resolution) => resolution.checkpoint_id));
  const open = [...view.checkpoints.values()].find((checkpoint) => !answered.has(checkpoint.checkpoint_id));
  const report = reportIn(events);

  return {
    run_id: runId,
    status: view.hunt.status,
    outcome: view.hunt.outcome,
    reason: why(view.hunt),
    iteration: view.hunt.iteration,
    cost_usd: view.hunt.cost_usd,
    budgets: view.hunt.budgets,
    hypotheses: [...view.hypotheses.values()].map((hypothesis) => standing(hypothesis, view)),
    evidence_count: view.evidence.size,
    // Ledger order reversed rather than sorted on captured_at, which ties within a
    // millisecond.
    evidence: [...view.evidence.values()]
      .reverse()
      .slice(0, EVIDENCE_SHOWN)
      .map((record) => evidenceView(record, view.links)),
    open_checkpoint: open === undefined ? null : openCheckpoint(open),
    report,
    report_markdown: report === null ? null : renderReport(report, view, narrativeIn(events)),
    narrative: narrativeView(narrativeIn(events)),
    handoffs: events.filter((event) => event.kind === "handoff").map((event) => event.payload as Handoff),
    open_questions: [...view.questions.values()]
      .filter((question) => question.status === "open")
      .map(({ question_id, question, entity_key, hypothesis_id, spawned_iteration }) => ({
        question_id,
        question,
        entity_key,
        hypothesis_id,
        spawned_iteration,
      })),
    moves: [...view.decisions].reverse().slice(0, MOVES_SHOWN).map(moveView),
  };
}

// Enough to read a whole hunt's reasoning, bounded for the reason the evidence is.
export const MOVES_SHOWN = 50;

function moveView(record: DecisionRecord): MoveView {
  const { decision } = record;
  return {
    decision_id: record.decision_id,
    iteration: record.iteration,
    action: decision.action,
    rationale: decision.rationale,
    target_entity: decision.target_entity ?? null,
    target_hypothesis_id: decision.target_hypothesis_id ?? null,
    query_intent: decision.query_intent ?? "",
    worker_agent_id: decision.worker_agent_id ?? null,
    evidence_citations: decision.evidence_citations ?? [],
    cost_usd: record.cost_usd,
    rejected_attempts: record.rejected_attempts ?? [],
    created_at: record.created_at,
  };
}

// The last one written. A run that resumed past its own terminal wrote a second,
// and the later one is the report of the hunt that actually happened.
function reportIn(events: readonly HuntEvent[]): HuntReport | null {
  const finalized = events.filter((event) => event.kind === "finalize");
  const last = finalized.at(-1);
  return last === undefined ? null : (last.payload as HuntReport);
}

// The latest account, so a regenerate supersedes without the earlier ones leaving the
// record. Read off the events, which the fold deliberately holds neither of.
function narrativeIn(events: readonly HuntEvent[]): Narrative | null {
  const written = events.filter((event) => event.kind === "narrative");
  const last = written.at(-1);
  return last === undefined ? null : (last.payload as Narrative);
}

function narrativeView(held: Narrative | null): HuntNarrative | null {
  if (held === null) return null;
  return {
    summary: held.summary,
    what_happened: held.what_happened,
    next_steps: stepsOf(held.next_steps),
    model_id: held.model_id,
    written_at: held.written_at,
  };
}

// A parked hunt is asked why it stopped, a terminal one why it ended, and the two
// are different fields: a hunt that resumed and later ended still holds both.
function why(hunt: HuntState): string {
  return (hunt.status === "terminal" ? hunt.termination_reason : hunt.parked_reason) ?? "";
}

// Enough to see what a hunt has been doing, without shipping a whole run's transcript
// on a five-second poll.
export const EVIDENCE_SHOWN = 50;

function evidenceView(
  record: EvidenceRecord,
  links: readonly { evidence_id: string; hypothesis_id: string; relation: LinkRelation }[],
): EvidenceView {
  return {
    evidence_id: record.evidence_id,
    iteration: record.iteration,
    source_system: record.source_system,
    summary: record.summary,
    why_notable: record.why_notable,
    salience: record.salience,
    attack_technique: record.attack_technique ?? null,
    attacker_influenceable: record.attacker_influenceable,
    sensor_attested: sensorAttested(record),
    rests_on: record.rests_on ?? [],
    instruction_like: record.instruction_like,
    provenance: record.provenance,
    is_gap: isGap(record),
    gap_detail: typeof record.payload["failure_reason"] === "string" ? record.payload["failure_reason"] : null,
    captured_at: record.captured_at,
    bears_on: links
      .filter((link) => link.evidence_id === record.evidence_id)
      .map((link) => ({ hypothesis_id: link.hypothesis_id, relation: link.relation })),
  };
}

function standing(hypothesis: Hypothesis, view: Projection): HypothesisStanding {
  const { hypothesis_id, statement, status, attack_technique, resolution_reason, provenance } = hypothesis;
  return {
    hypothesis_id,
    statement,
    status,
    attack_technique,
    techniques_cited: citedTechniques(view, hypothesis_id),
    resolution_reason,
    provenance,
  };
}
