import { resolutionOf, type Checkpoint, type Resolution } from "./checkpoints.js";
import { suppressedEntities } from "./digest.js";
import type { Projection } from "./ledger.js";
import { citedTechniques, isGap, sensorAttested, unruledObservations } from "./strength.js";
import { renderNarrative, type Narrative } from "./narrative.js";
import type {
  Budgets,
  EvidenceRecord,
  EvidenceStrength,
  Handoff,
  HuntOutcome,
  HypothesisStatus,
  LinkRelation,
} from "./types.js";

// The deliverable, derived. A hunt that ends without one is a hunt that never
// happened — and "we found nothing, here is what we could not see" is an answer,

export interface HypothesisVerdict {
  hypothesis_id: string;
  statement: string;
  status: HypothesisStatus;
  resolution_reason: string | null;
  evidence_strength: EvidenceStrength | null;
}

export interface VisibilityGap {
  evidence_id: string;
  iteration: number;
  summary: string;
  // What went unanswered and for which claim, so a gap reads as a question the
  // hunt could not put, rather than as a tool that misbehaved.
  query_intent: string;
  hypothesis_id: string | null;
}

export interface BacklogQuestion {
  question_id: string;
  question: string;
  reason: string;
}

// Every point a human was asked, and what happened — including the ones policy
// answered. A supervised hunt that cannot show where it was supervised is a hunt
export interface CheckpointRecord {
  checkpoint_id: string;
  class: Checkpoint["checkpoint_class"];
  raised_iteration: number | undefined;
  question: string;
  resolution: Resolution | null;
}

export interface Suppression {
  entity_key: string;
  actor: string;
}

export interface HuntReport {
  hunt_id: string;
  name: string;
  outcome: HuntOutcome | null;
  reason: string;
  iterations: number;
  cost_usd: number;
  budgets: Budgets;
  created_at: string;
  terminated_at: string | null;
  hypotheses: HypothesisVerdict[];
  gaps: VisibilityGap[];
  // Parked hypotheses and the leads nobody pulled: the work this hunt did not do,
  // named so the next one can pick it up.
  parked_hypotheses: HypothesisVerdict[];
  backlog: BacklogQuestion[];
  checkpoints: CheckpointRecord[];
  // Still in force at the end. The revoked ones are on the ledger and out of this
  // list, because the report says what the hunt ran under, not what it was told.
  suppressions: Suppression[];
  handoffs: Handoff[];
  // Observations that reached no hypothesis at all. Absent on a legacy run,
  // which never asked the lead to rule on anything.
  unruled?: number;
}

// One entry per question, not per worker: a fan-out hands every worker the same
// query_intent. Presentation only -- buildReport's gaps array is untouched.
export interface AskedGap {
  iteration: number;
  hypothesis_id: string | null;
  query_intent: string;
  reasons: string[];
  workers: number;
}

export function groupedGaps(gaps: readonly VisibilityGap[]): AskedGap[] {
  const byQuestion = new Map<string, AskedGap>();
  for (const gap of gaps) {
    const key = `${gap.iteration}|${gap.hypothesis_id ?? ""}|${gap.query_intent}`;
    const held = byQuestion.get(key);
    if (held === undefined) {
      byQuestion.set(key, {
        iteration: gap.iteration,
        hypothesis_id: gap.hypothesis_id,
        query_intent: gap.query_intent,
        reasons: [gap.summary],
        workers: 1,
      });
      continue;
    }
    held.workers += 1;
    if (!held.reasons.includes(gap.summary)) held.reasons.push(gap.summary);
  }
  return [...byQuestion.values()];
}

function verdictOf(hypothesis: {
  hypothesis_id: string;
  statement: string;
  status: HypothesisStatus;
  resolution_reason: string | null;
  evidence_strength?: EvidenceStrength | null;
}): HypothesisVerdict {
  return {
    hypothesis_id: hypothesis.hypothesis_id,
    statement: hypothesis.statement,
    status: hypothesis.status,
    resolution_reason: hypothesis.resolution_reason,
    evidence_strength: hypothesis.evidence_strength ?? null,
  };
}

export function buildReport(projection: Projection): HuntReport {
  const { hunt } = projection;
  const hypotheses = [...projection.hypotheses.values()].map(verdictOf);

  const gaps = [...projection.evidence.values()]
    .filter(isGap)
    .map((record) => {
      const dispatch = projection.dispatches.get(record.dispatch_id ?? "");
      return {
        evidence_id: record.evidence_id,
        iteration: record.iteration,
        summary: record.summary,
        query_intent: dispatch?.query_intent ?? "",
        hypothesis_id: dispatch?.target_hypothesis_id ?? null,
      };
    })
    .sort((a, b) => (a.iteration === b.iteration ? a.evidence_id.localeCompare(b.evidence_id) : a.iteration - b.iteration));

  return {
    hunt_id: hunt.hunt_id,
    name: hunt.name,
    outcome: hunt.outcome,
    reason: hunt.termination_reason ?? "",
    iterations: hunt.iteration,
    cost_usd: hunt.cost_usd,
    budgets: hunt.budgets,
    created_at: hunt.created_at,
    terminated_at: hunt.terminated_at,
    hypotheses,
    gaps,
    parked_hypotheses: hypotheses.filter((hypothesis) => hypothesis.status === "parked"),
    backlog: [...projection.questions.values()]
      .filter((question) => question.status === "parked")
      .map((question) => ({
        question_id: question.question_id,
        question: question.question,
        reason: question.closed_reason ?? "",
      })),
    checkpoints: [...projection.checkpoints.values()].map((checkpoint) => ({
      checkpoint_id: checkpoint.checkpoint_id,
      class: checkpoint.checkpoint_class,
      raised_iteration: checkpoint.raised_iteration,
      question: checkpoint.question,
      resolution: resolutionOf(projection, checkpoint.checkpoint_id) ?? null,
    })),
    suppressions: [...suppressedEntities(projection)].map(([entity_key, actor]) => ({ entity_key, actor })),
    handoffs: [...projection.handoffs],
    ...(projection.hunt.spec.hypothesis_loop ? { unruled: unruledObservations(projection) } : {}),
  };
}

const STATUS_ORDER: Record<HypothesisStatus, number> = {
  handed_off: 0,
  proven: 1,
  disproven: 2,
  inconclusive: 3,
  parked: 4,
  active: 5,
};

function strengthLine(strength: EvidenceStrength): string {
  return [
    `${strength.corroborating_sources} corroborating source system(s)`,
    `${strength.contradicting_records} contradicting record(s)`,
    `${strength.open_gaps} open gap(s)`,
    strength.attacker_influenceable_only ? "support is attacker-influenceable only" : "support is not attacker-authored alone",
    strength.survived_disconfirmation ? "survived disconfirmation" : "did not survive disconfirmation",
  ].join(", ");
}

// The one line an operator reads first. A hunt that proved nothing says so
// plainly and says why — an unread report is the same as no report.
function headline(report: HuntReport): string {
  // handed_off counts: it is a proven hypothesis that has moved to incident
  // response, and a report that read "nothing was proven" over an escalation
  const proven = report.hypotheses.filter(
    (hypothesis) => hypothesis.status === "proven" || hypothesis.status === "handed_off",
  );
  if (proven.length > 0) {
    const escalated = report.handoffs.length === 0 ? "" : ` ${report.handoffs.length} was escalated to incident response.`;
    return `${proven.length} hypothesis(es) reached a verdict of proven; each survived the argue-the-null pass.${escalated}`;
  }
  if (report.outcome === "data_starved") {
    return (
      `Nothing was proven, and the hunt could not see well enough to say so honestly: ` +
      `${report.gaps.length} visibility gap(s) closed hypotheses that were never cleared.`
    );
  }
  if (report.outcome === "completed") {
    // "Reached the end of its frontier" is a claim about coverage, which a hunt
    // concluded early cannot make.
    const left = report.backlog.length + report.parked_hypotheses.length;
    if (left > 0) {
      return `Nothing was proven, and the hunt did not run out of things to try: ${left} lead(s) were left open. See the parked backlog below.`;
    }
    return "Nothing was proven. The hunt reached the end of its frontier without finding support that cleared the bar.";
  }
  if (report.outcome === "failed") {
    return "Nothing was proven: the hunt stopped on a fault, not on an answer. What it had gathered by then is below.";
  }
  return `Nothing was proven; the hunt ended ${report.outcome ?? "without an outcome"} with its hypotheses unresolved.`;
}

// How many records the report prints: enough for the whole trail, bounded so a long
// run does not bury the verdicts.
export const FINDINGS_SHOWN = 40;

// What the hunt actually saw, which the verdicts and gaps between them never say.
// Derived from the projection, so buildReport's frozen object is untouched.
function findings(projection: Projection): string[] {
  const gathered = [...projection.evidence.values()].filter((record) => !isGap(record)).reverse();
  const lines = [`## What the hunt found (${gathered.length})`, ""];
  if (gathered.length === 0) return [...lines, "Nothing came back that was not a blind spot.", ""];

  const shown = gathered.slice(0, FINDINGS_SHOWN);
  lines.push(shown.length < gathered.length ? `The ${shown.length} most recent, newest first.` : "Newest first.", "");
  for (const record of shown) {
    const links = projection.links.filter((link) => link.evidence_id === record.evidence_id);
    // Evidence attached to nothing is the case worth printing.
    const bears = links.length === 0 ? "bears on nothing" : links.map((link) => `${link.relation} ${link.hypothesis_id}`).join(", ");
    const caveat = sensorAttested(record) ? "" : ", nothing sensor-attested";
    lines.push(`- **iteration ${record.iteration}** (${record.source_system || "unattributed"}, ${record.salience}${caveat}) — ${record.summary}`);
    if (record.why_notable) lines.push(`  - ${record.why_notable}`);
    lines.push(`  - ${bears}`);
  }
  return [...lines, ""];
}

// projection is optional and never touches HuntReport's own shape, which the ADR 0012
// goldens compare exactly: citations are derived fresh from the ledger at render time.
export function renderReport(report: HuntReport, projection?: Projection, narrative?: Narrative | null): string {
  const lines: string[] = [
    `# Hunt report — ${report.name}`,
    "",
    `- **Outcome:** ${report.outcome ?? "not terminated"}`,
    `- **Hunt:** ${report.hunt_id}`,
    `- **Iterations:** ${report.iterations} of ${report.budgets.max_iterations}`,
    `- **Cost:** $${report.cost_usd.toFixed(4)} of $${report.budgets.max_cost_usd.toFixed(2)}`,
    `- **Started:** ${report.created_at}`,
    `- **Ended:** ${report.terminated_at ?? "still running"}`,
  ];
  if (report.reason) lines.push(`- **Why it ended:** ${report.reason}`);
  // Before the verdicts, since everything below is the record it was written from.
  // Absent when the narrator could not run, leaving the report as it was.
  if (narrative !== undefined && narrative !== null) lines.push("", ...renderNarrative(narrative));
  lines.push("", headline(report), "", "## Verdicts", "");

  const ordered = [...report.hypotheses].sort(
    (a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || a.hypothesis_id.localeCompare(b.hypothesis_id),
  );
  for (const hypothesis of ordered) {
    lines.push(`### ${hypothesis.hypothesis_id} — ${hypothesis.status}`, "", hypothesis.statement, "");
    if (hypothesis.resolution_reason) lines.push(`_${hypothesis.resolution_reason}_`, "");
    // What evidence actually cited, distinct from the playbook's own label: a hunt
    // can find something its definition never named.
    const observed = projection === undefined ? [] : citedTechniques(projection, hypothesis.hypothesis_id);
    if (observed.length > 0) lines.push(`**Techniques cited by evidence:** ${observed.join(", ")}`, "");
    if (hypothesis.evidence_strength !== null) {
      lines.push(`Evidence strength at verdict: ${strengthLine(hypothesis.evidence_strength)}.`, "");
    }
  }

  if (projection !== undefined) lines.push(...findings(projection));

  lines.push(`## Visibility gaps (${report.gaps.length})`, "");
  if (report.gaps.length === 0) {
    lines.push("None: every query the hunt wanted to run came back.", "");
  } else {
    lines.push("Questions the hunt could not answer. Each is a blind spot, not a finding.", "");
    for (const asked of groupedGaps(report.gaps)) {
      const bearing = asked.hypothesis_id === null ? "unattributed" : asked.hypothesis_id;
      const workers = asked.workers > 1 ? ` (${asked.workers} workers)` : "";
      lines.push(`- iteration ${asked.iteration} (${bearing})${workers}: ${asked.query_intent || asked.reasons[0]}`);
      if (asked.query_intent !== "") for (const reason of asked.reasons) lines.push(`  - ${reason}`);
    }
    lines.push("");
  }

  lines.push("## Parked backlog", "");
  if (report.parked_hypotheses.length === 0 && report.backlog.length === 0) {
    lines.push("Nothing parked: no hypothesis was abandoned and no lead was left on the frontier.", "");
  }
  if (report.parked_hypotheses.length > 0) {
    lines.push("### Hypotheses", "");
    for (const hypothesis of report.parked_hypotheses) {
      lines.push(`- ${hypothesis.statement} — ${hypothesis.resolution_reason ?? "parked"}`);
    }
    lines.push("");
  }
  if (report.backlog.length > 0) {
    lines.push("### Open questions", "");
    for (const question of report.backlog) {
      lines.push(`- ${question.question}${question.reason ? ` — ${question.reason}` : ""}`);
    }
    lines.push("");
  }

  if (report.unruled !== undefined && report.unruled > 0) {
    lines.push(`## Unruled observations (${report.unruled})`, "", "Evidence the hunt ended before ruling on.", "");
  }

  if (report.handoffs.length > 0) {
    lines.push("## Escalated to incident response", "");
    for (const handoff of report.handoffs) {
      lines.push(`- ${handoff.case_id} (${handoff.hypothesis_id}, iteration ${handoff.iteration}) — ${handoff.rationale}`);
      if (handoff.case_file) lines.push(`  case file: ${handoff.case_file}`);
    }
    lines.push("");
  }

  // Where a human was in the loop, and where policy stood in for one. Rendered
  // only when something was raised, so a hunt nobody supervised does not grow a
  if (report.checkpoints.length > 0) {
    lines.push("## Checkpoints", "");
    for (const checkpoint of report.checkpoints) {
      const resolution = checkpoint.resolution;
      const answer =
        resolution === null
          ? "**still pending**"
          : `${resolution.answer} by ${resolution.actor}${resolution.text ? ` — ${resolution.text}` : ""}`;
      lines.push(`- ${checkpoint.class} (iteration ${checkpoint.raised_iteration}): ${checkpoint.question} → ${answer}`);
    }
    lines.push("");
  }

  if (report.suppressions.length > 0) {
    lines.push(
      "## Operator suppressions",
      "",
      "Entities an operator marked known-benign. The evidence mentioning them is untouched;",
      "the hunt stopped opening new work on them from the moment each was recorded.",
      "",
    );
    for (const suppression of report.suppressions) {
      lines.push(`- ${suppression.entity_key} — ${suppression.actor}`);
    }
    lines.push("");
  }

  return `${lines.join("\n").trimEnd()}\n`;
}

// What an IR responder is handed: the claim, the numbers behind it, the records
// it rests on, and — the part a finding usually loses — what the hunt could not
// What the finding rests on, and who chose it. A reader deciding whether to act on a
// claim needs to know which half of it the adversary could have written.
function restsOnLine(record: EvidenceRecord): string[] {
  const rests = record.rests_on ?? [];
  if (rests.length === 0) {
    return [sensorAttested(record) ? "" : "_This record rests on content an adversary could have written._"];
  }
  const named = rests.map((basis) => `${basis.field} (${basis.authored})`).join(", ");
  return [`_Rests on: ${named}._`];
}

// What a responder has to re-run: the payload is the answer, this is the question.
// An enriched record has no dispatch, and a chain is not a query anyone chose.
function queriesBehind(projection: Projection, record: EvidenceRecord): string[] {
  const dispatch = record.dispatch_id === null ? undefined : projection.dispatches.get(record.dispatch_id);
  if (dispatch === undefined || dispatch.calls.length === 0) return [];
  return [
    "Queries behind it:",
    "",
    ...dispatch.calls.map((call) => `- \`${call.tool}\` — \`${call.arguments}\``),
    "",
  ];
}

export function renderCaseFile(projection: Projection, handoff: Handoff): string {
  const hypothesis = projection.hypotheses.get(handoff.hypothesis_id);
  const report = buildReport(projection);
  const supporting = projection.links
    .filter((link) => link.hypothesis_id === handoff.hypothesis_id)
    .map((link) => ({ relation: link.relation, record: projection.evidence.get(link.evidence_id) }))
    .filter((entry): entry is { relation: LinkRelation; record: EvidenceRecord } => entry.record !== undefined);

  const lines: string[] = [
    `# IR case ${handoff.case_id} — ${projection.hunt.name}`,
    "",
    `- **Hunt:** ${projection.hunt.hunt_id}`,
    `- **Hypothesis:** ${handoff.hypothesis_id}`,
    `- **Escalated:** ${handoff.created_at} (iteration ${handoff.iteration})`,
    "",
    "## The claim",
    "",
    hypothesis?.statement ?? "(the hypothesis is no longer on the ledger)",
    "",
    `_Escalated because:_ ${handoff.rationale}`,
    "",
  ];

  if (hypothesis?.resolution_reason) lines.push(`_Verdict:_ ${hypothesis.resolution_reason}`, "");
  if (hypothesis?.evidence_strength) {
    lines.push(`Evidence strength at verdict: ${strengthLine(hypothesis.evidence_strength)}.`, "");
  }

  lines.push("## Evidence trail", "");
  if (supporting.length === 0) {
    lines.push("Nothing is linked to this hypothesis, which is itself worth knowing before acting on it.", "");
  }
  for (const { relation, record } of supporting) {
    lines.push(
      `### ${record.evidence_id} — ${relation} (${record.source_system}, iteration ${record.iteration})`,
      "",
      record.summary,
      "",
      ...restsOnLine(record),
      "```json",
      JSON.stringify(record.payload, null, 2),
      "```",
      "",
      ...queriesBehind(projection, record),
    );
  }

  // The responder is about to act on this. What the hunt could not see bounds
  // what it found, and shipping the finding without it is how a blind spot
  lines.push(`## What the hunt could not see (${report.gaps.length})`, "");
  if (report.gaps.length === 0) {
    lines.push("Every query the hunt wanted to run came back.", "");
  }
  for (const gap of report.gaps) {
    lines.push(`- iteration ${gap.iteration}: ${gap.query_intent || gap.summary} — ${gap.summary}`);
  }

  return `${lines.filter((line, index) => line !== "" || lines[index - 1] !== "").join("\n").trimEnd()}\n`;
}
