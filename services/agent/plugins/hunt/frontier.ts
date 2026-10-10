import type { Projection } from "./ledger.js";
import type { EvidenceRecord, LinkRelation, OpenQuestion, Salience } from "./types.js";

// The hunt's frontier: open questions ranked by what answering could move, and entities an
// operator said to stop chasing. Folds read by termination, the report and the brief.

const RANK: Record<Salience, number> = { routine: 0, notable: 1, anomalous: 2 };

// An operator's known-benign calls, folded in order so a later revoke lifts an
// earlier suppression. Nothing is deleted either way: both directives stay on
export function suppressedEntities(projection: Projection): Map<string, string> {
  const suppressed = new Map<string, string>();
  for (const directive of projection.directives) {
    if (directive.kind !== "benign") continue;
    const entityKey = directive.entity_key ?? directive.text.trim();
    if (entityKey === "") continue;
    if (directive.revoke === true) suppressed.delete(entityKey);
    else suppressed.set(entityKey, directive.actor);
  }
  return suppressed;
}

// Leads an operator asked for next. A flag rather than a stored score: the score
// is a fold over the ledger and would go stale, and an operator's "do this one"
export function boostedQuestions(projection: Projection): Set<string> {
  return new Set(
    projection.directives
      .filter((directive) => directive.kind === "boost" && directive.question_id !== undefined)
      .map((directive) => directive.question_id as string),
  );
}

const W_NOVEL = 3;
const W_HYPOTHESIS = 2;
const W_SALIENCE = 2;
const W_RECENCY = 1;
const HYPOTHESIS_CAP = 3;
const RECENCY_SPAN = 3;
// Splitting outranks bearing, and the two together spend what bearing alone did.
const W_DISCRIMINATE = 2;
const W_BEARING = 1;
const SPLIT_CAP = 2;
const BEARING_CAP = 2;

// A lead with no entity has only its own text to be compared on.
function coverage(question: OpenQuestion): string {
  return question.entity_key ?? question.question;
}

// A worker's follow-up names its dispatch rather than one record, so the features
// are read over everything that dispatch found.
function behind(question: OpenQuestion, projection: Projection): EvidenceRecord[] {
  const cited = projection.evidence.get(question.spawning_evidence_id ?? "");
  if (cited !== undefined) return [cited];
  if (question.spawning_dispatch_id === null) return [];
  return [...projection.evidence.values()].filter((record) => record.dispatch_id === question.spawning_dispatch_id);
}

// ponytail: the salience feature reads the stored tag rather than the promoted
// floor, which needs the graph and links buildDigest assembles separately. A
function priority(question: OpenQuestion, projection: Projection, iteration: number, taken: ReadonlySet<string>, active: ReadonlySet<string>): number {
  const spawning = behind(question, projection);
  const ids = new Set(spawning.map((record) => record.evidence_id));
  const bearing = new Set(
    projection.links.filter((link) => ids.has(link.evidence_id) && active.has(link.hypothesis_id)).map((link) => link.hypothesis_id),
  );

  return (
    (taken.has(coverage(question)) ? 0 : W_NOVEL) +
    W_HYPOTHESIS * Math.min(bearing.size, HYPOTHESIS_CAP) +
    W_SALIENCE * Math.max(0, ...spawning.map((record) => RANK[record.salience])) +
    W_RECENCY * Math.max(0, RECENCY_SPAN - (iteration - question.spawned_iteration))
  );
}

// Bearing counts hypotheses a lead touches; splitting counts hypotheses it would
// move in opposite directions. Only the second can separate the field.
function discriminating(question: OpenQuestion, projection: Projection, iteration: number, taken: ReadonlySet<string>, active: ReadonlySet<string>): number {
  const spawning = behind(question, projection);
  const ids = new Set(spawning.map((record) => record.evidence_id));
  const bearingOn = (relation: LinkRelation) =>
    new Set(
      projection.links
        .filter((link) => ids.has(link.evidence_id) && active.has(link.hypothesis_id) && link.relation === relation)
        .map((link) => link.hypothesis_id),
    );

  const supports = bearingOn("supports");
  const weakens = bearingOn("weakens");
  const split = Math.min(supports.size, weakens.size);
  const bearing = new Set([...supports, ...weakens]).size;

  // Same six points the bearing term spent, redistributed, so a score still tops
  // out at 16 and priority_floor keeps the meaning it was calibrated against.
  return (
    (taken.has(coverage(question)) ? 0 : W_NOVEL) +
    W_DISCRIMINATE * Math.min(split, SPLIT_CAP) +
    W_BEARING * Math.min(bearing, BEARING_CAP) +
    W_SALIENCE * Math.max(0, ...spawning.map((record) => RANK[record.salience])) +
    W_RECENCY * Math.max(0, RECENCY_SPAN - (iteration - question.spawned_iteration))
  );
}

// Which scorer a run uses is fixed by its spec. A ledger written before the
// discrimination scorer existed replays under the one that actually ranked it.
function scorerOf(projection: Projection) {
  return projection.hunt.spec.hypothesis_loop ? discriminating : priority;
}

export interface ScoredQuestion {
  question: OpenQuestion;
  score: number;
  // An operator pinned this one. Kept beside the score rather than folded into
  // it, so the floor termination measures against stays a statement about the
  boosted: boolean;
}

// The frontier ranked rather than taken in arrival order. Every feature is folded
// from the ledger: a stored score would be stale the moment the next dispatch
export function scoredFrontier(projection: Projection, iteration: number): ScoredQuestion[] {
  const questions = [...projection.questions.values()];
  // Closed once taken, so the closed leads are the execution log. A parked lead
  // was never pulled, so it covers nothing.
  const taken = new Set(questions.filter((question) => question.status === "closed").map(coverage));
  const active = new Set(
    [...projection.hypotheses.values()].filter((h) => h.status === "active").map((h) => h.hypothesis_id),
  );
  const boosted = boostedQuestions(projection);
  const score = scorerOf(projection);

  return questions
    .filter((question) => question.status === "open")
    .map((question) => ({
      question,
      score: score(question, projection, iteration, taken, active),
      boosted: boosted.has(question.question_id),
    }))
    // A boost outranks every score: an operator saying "look at this next" is
    // not competing with the controller's ranking, it is overriding it — until a
    .sort((a, b) =>
      a.boosted !== b.boosted
        ? Number(b.boosted) - Number(a.boosted)
        : b.score === a.score
          ? a.question.question_id.localeCompare(b.question.question_id)
          : b.score - a.score,
    );
}

export function rankFrontier(projection: Projection, iteration: number): OpenQuestion[] {
  return scoredFrontier(projection, iteration).map((entry) => entry.question);
}
