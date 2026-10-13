import { scrub } from "../../core/security.js";
import { sensorAttested } from "./strength.js";
import type { NullCheckInput } from "./types.js";

// What the critic is shown. Evidence carries its id, because the critic is asked to cite it.
const EVIDENCE_CAP = 4_000;
const DIRECTIVE_CAP = 2_000;

// Operator text is direction, but a model cannot tell a person's words from a forged
// section, so it is scrubbed, capped and kept to one line, which opens no heading.
export function operatorLine(text: string): string {
  return scrub(text, DIRECTIVE_CAP).replace(/\s*\n\s*/g, " ");
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
