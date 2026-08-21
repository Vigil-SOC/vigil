import { scrub } from "../../core/security.js";
import { sensorAttested } from "./strength.js";
import type { Digest, DispatchRequest, EntityView, EvidenceView, HypothesisView, NullCheckInput } from "./types.js";

// The digest as the lead reads it: buildDigest decides what is in it, this only
// how it is written. Evidence carries its id, because the lead is asked to cite them.
const EVIDENCE_CAP = 4_000;

// One delimited shape for anything a model reads as evidence, expansions included:
// a raw payload arriving undelimited would read as the digest's own voice.
function evidenceBlock(id: string, body: string): string {
  return `<vigil:evidence id="${id}">\n${body}\n</vigil:evidence>`;
}

function section(heading: string, body: string): string {
  return body.trim() === "" ? "" : `## ${heading}\n${body.trim()}`;
}

function hypothesis(view: HypothesisView): string {
  return `- ${view.hypothesis_id} (${view.status}): ${view.statement}`;
}

function evidence(view: EvidenceView): string {
  const body = scrub(`${view.summary}\n${view.why_notable}`, EVIDENCE_CAP);
  // instruction_like is stated rather than acted on: the lead is told the text
  // reads like direction, and the block is what says it is still only data.
  const flagged = view.instruction_like ? ' instruction_like="true"' : "";
  return [
    `<vigil:evidence id="${view.evidence_id}" source="${view.source_system}" salience="${view.salience}"${flagged}>`,
    body,
    `</vigil:evidence>`,
  ].join("\n");
}

function entity(view: EntityView): string {
  const suppressed = view.suppressed === true ? ", called benign by an operator" : "";
  return `- ${view.type}:${view.value} (seen ${view.count}x${suppressed})`;
}

function weakening(weakens: Digest["weakens"]): string {
  const entries = Object.entries(weakens).filter(([, views]) => views.length > 0);
  if (entries.length === 0) return "Nothing yet weakens any hypothesis, which means the hunt has looked one way.";
  return entries.map(([id, views]) => `Against ${id}:\n${views.map(evidence).join("\n")}`).join("\n\n");
}

// Two ceilings: which one binds is the fact worth having, and the slack on the other
// matters too, since an operator reading the same line is who can lift it.
function budgetLine(remaining: Digest["budget_remaining"]): string {
  const cash = `$${remaining.cost_usd.toFixed(2)}`;
  if (remaining.iterations <= 0) {
    return `This is the last turn: none remain after this decision. ${cash} of the allowance is still unspent, so turns are what bound this hunt, not money.`;
  }
  if (remaining.cost_usd <= 0) {
    return `The allowance is spent: ${remaining.iterations} turn(s) remain but there is nothing left to spend on them.`;
  }
  return `${remaining.iterations} turn(s) after this one and ${cash} left; whichever runs out first ends the hunt.`;
}

export function renderDigest(digest: Digest): string {
  const budget = budgetLine(digest.budget_remaining);
  const focus = [digest.focus.entity, digest.focus.hypothesis].filter((part) => part !== null).join(" / ");
  const omitted =
    digest.omitted.count === 0
      ? ""
      : `${digest.omitted.count} routine record(s) compressed out: ${digest.omitted.evidence_ids.join(", ")}`;

  return [
    `# ${digest.hunt_name} — iteration ${digest.iteration}`,
    digest.narrative,
    section("Hypotheses", digest.hypotheses.map(hypothesis).join("\n")),
    section("Recent evidence", digest.recent_evidence.map(evidence).join("\n\n")),
    section("Counter-evidence", weakening(digest.weakens)),
    section("Entities seen", digest.entities.map(entity).join("\n")),
    section("Focus", focus === "" ? "nothing in particular" : focus),
    section("Pivot candidates", digest.pivot_candidates.map(entity).join("\n")),
    section("Compressed", omitted),
    section(
      "Expanded on request",
      digest.expansions.map((one) => evidenceBlock(one.evidence_id, scrub(one.payload, EVIDENCE_CAP))).join("\n\n"),
    ),
    section("Open questions", digest.open_questions.map((one) => `- ${one}`).join("\n")),
    // Last, and named as direction: everything above is data, and the lead is
    // told which is which rather than being left to infer it from position.
    section("Operator directives", digest.directives.map((one) => `- ${one}`).join("\n")),
    section("Notes", digest.notes.map((one) => `- ${one}`).join("\n")),
    section("Budget remaining", budget),
  ]
    .filter((part) => part !== "")
    .join("\n\n");
}


// What a worker is told. The hypothesis ids are here because the prompt asks it to
// cite them, and the scope because without one a worker invents its own bounds.
export function renderDispatch(request: DispatchRequest, narrative: string): string {
  const lines = ["# Query intent", request.query_intent];
  if (request.focus) lines.push("", "## Your focus", request.focus);
  if (request.target_hypothesis_id !== null) {
    lines.push("", `This bears on hypothesis ${request.target_hypothesis_id}.`);
  }
  // Defensive: this renders a prompt, and a throw would fail the whole dispatch.
  const scope = request.scope ?? {};
  if (Object.keys(scope).length > 0) lines.push("", "## Scope", JSON.stringify(scope));
  if (narrative) lines.push("", "## Scenario", narrative);
  return lines.join("\n");
}

// The critic argues against the records themselves, so they arrive delimited and
// carrying the two attributes the argument turns on. Raw JSON would read as our voice.
// The basis, flattened for an attribute, and absent when the worker named none: the
// critic's whole job is to argue the benign case, and which half of a record the
// adversary authored is the argument's best lever.
function basisAttr(record: { rests_on?: { field: string; authored: string }[] }): string {
  const named = (record.rests_on ?? []).map((basis) => `${basis.field}:${basis.authored}`).join(" ");
  return named === "" ? "" : ` rests_on="${scrub(named, EVIDENCE_CAP)}"`;
}

export function renderNullCheck(check: NullCheckInput): string {
  const lines = [
    "# Hypothesis put up for a verdict",
    `[${check.hypothesis_id}] ${check.statement}`,
    "",
    "## Everything the hunt has linked to it",
  ];

  if (check.evidence.length === 0) lines.push("Nothing is linked to this hypothesis.");
  for (const { relation, record } of check.evidence) {
    lines.push(
      `<vigil:evidence id="${record.evidence_id}" relation="${relation}" source="${record.source_system}" ` +
        `sensor_attested="${sensorAttested(record)}"${basisAttr(record)}>`,
      scrub(record.summary, EVIDENCE_CAP),
      record.why_notable ? `why notable: ${scrub(record.why_notable, EVIDENCE_CAP)}` : "",
      scrub(JSON.stringify(record.payload), EVIDENCE_CAP),
      "</vigil:evidence>",
    );
  }

  if (check.narrative) lines.push("", "## Scenario", check.narrative);
  return lines.filter((line) => line !== "").join("\n");
}


// The playbook's standing brief plus this run's own. One function because all three
// roles read it, and a worker told less than the lead queries an estate it does not know.
export function narrativeOf(spec: { narrative: string; prompt: string }): string {
  return [spec.narrative, spec.prompt && `## What this run is about\n\n${spec.prompt}`]
    .filter((part) => part)
    .join("\n\n");
}
