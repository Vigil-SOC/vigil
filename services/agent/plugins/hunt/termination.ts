import { BASE_RATE_PROVENANCE } from "./opening.js";
import { scoredFrontier } from "./frontier.js";
import type { Projection } from "./ledger.js";
import { DEFAULT_VERDICTS, type Termination, type Verdicts } from "./config.js";
import { openGaps } from "./strength.js";
import type { Hypothesis, OpenQuestion } from "./types.js";

// Sub-floor leads ride along on a passing verdict so the caller can close them as
// backlog in one place. data_starved carries them for the same reason completed
export type TerminationVerdict =
  | { outcome: "completed"; park: OpenQuestion[] }
  | { outcome: "data_starved"; park: OpenQuestion[] }
  // Why the hunt must continue. A CONCLUDE that lands here is refused, not rejected.
  | { outcome: null; blocked_by: string };

// A hypothesis the hunt closed because it could not look, rather than because it
// looked and found nothing. Read off the strength snapshot the verdict wrote, not
function gapLocked(projection: Projection, hypothesis: Hypothesis, verdicts: Verdicts): boolean {
  const strength = hypothesis.evidence_strength;
  if (strength === undefined || strength === null) return false;
  // A verdict that could not look leaves the hypothesis as it stands, so the snapshot
  // alone is not enough: the gaps must still be open. Older ledgers closed it
  // inconclusive, and that snapshot is the whole of what they hold.
  if (hypothesis.status === "inconclusive") return strength.open_gaps >= verdicts.gap_lock_threshold;
  return (
    hypothesis.status === "active" &&
    strength.open_gaps >= verdicts.gap_lock_threshold &&
    openGaps(projection, hypothesis.hypothesis_id) >= verdicts.gap_lock_threshold
  );
}

function blockingActive(projection: Projection, verdicts: Verdicts): TerminationVerdict | null {
  const active = [...projection.hypotheses.values()].find(
    (hypothesis) => hypothesis.status === "active" && !gapLocked(projection, hypothesis, verdicts),
  );
  if (active === undefined) return null;
  return { outcome: null, blocked_by: `${active.hypothesis_id} is still active: ${active.statement}` };
}

// One dominates, or none can. A claim that beat the null is the first; every
// remaining claim being blind is the second, and it is an answer rather than a failure.
function dominates(projection: Projection, hypotheses: readonly Hypothesis[], verdicts: Verdicts): TerminationVerdict | null {
  const settled = (hypothesis: Hypothesis) => hypothesis.status === "proven" || hypothesis.status === "handed_off";
  if (hypotheses.some(settled)) return null;

  const active = hypotheses.filter((hypothesis) => hypothesis.status === "active");
  const contenders = active.filter(
    (hypothesis) => hypothesis.provenance !== BASE_RATE_PROVENANCE && !gapLocked(projection, hypothesis, verdicts),
  );
  if (contenders.length === 0) return null;

  // Every claim that could still have beaten the null is blind, so no further
  // query changes the answer. Saying so is the point; guessing instead is the failure.
  if (hypotheses.filter((h) => h.provenance !== BASE_RATE_PROVENANCE).every((h) => gapLocked(projection, h, verdicts))) return null;

  const first = contenders[0]!;
  return { outcome: null, blocked_by: `${first.hypothesis_id} is still active: ${first.statement}` };
}

// The controller's own answer to "may this hunt stop?", computed from the
// projection exactly like evidence_strength — never from anything the Hunt Lead
export function terminationVerdict(
  projection: Projection,
  iteration: number,
  config: Termination,
  verdicts: Verdicts = DEFAULT_VERDICTS,
): TerminationVerdict {
  const hypotheses = [...projection.hypotheses.values()];

  // Under the hypothesis loop the null is always active until something beats it,
  // so "nothing active" would never come true and the hunt could never stop.
  const dominance = projection.hunt.spec.hypothesis_loop
    ? dominates(projection, hypotheses, verdicts)
    : blockingActive(projection, verdicts);
  if (dominance !== null) return dominance;

  const frontier = scoredFrontier(projection, iteration);
  const above = frontier.find((entry) => entry.score >= config.priority_floor);
  if (above !== undefined) {
    return {
      outcome: null,
      blocked_by:
        `open question ${above.question.question_id} scores ${above.score}, at or above the priority ` +
        `floor of ${config.priority_floor}: ${above.question.question}`,
    };
  }

  // Everything left on the frontier is below the floor, so the hunt is done
  // spending on it and it becomes the backlog deliverable.
  const park = frontier.map((entry) => entry.question);

  // Outranks completed when both match: a hunt that could not see is not a hunt
  // that finished, and reporting it as finished is how a blind spot becomes a
  const starved =
    hypotheses.some((hypothesis) => gapLocked(projection, hypothesis, verdicts)) &&
    !hypotheses.some((hypothesis) => hypothesis.status === "proven");

  return starved ? { outcome: "data_starved", park } : { outcome: "completed", park };
}
