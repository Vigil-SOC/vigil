import { describe, expect, it } from "vitest";
import { DEFAULT_FOLD } from "../../core/context.js";
import { asHarnessEvents, gunzipped } from "../support/historical.js";
import { fold } from "../../workflows/hunt/ledger.js";
import { DEFAULT_DIGEST, digestOf } from "../../workflows/hunt/config.js";
import { buildDigest, rankFrontier } from "../../workflows/hunt/digest.js";
import { renderDigest } from "../../workflows/hunt/render.js";

// A real budget-stopped run: 151 records, most promoted past routine by the floor,
// and 82 leads left open. Unbounded, its final digest rendered to 159,176 chars,
// past the request ceiling on its own (#1437).
const RUN = "hunt-462b6e9d6d56";

// Half the ceiling, so the history assemble() folds in beside the task still fits.
const RENDERED_MAX = 60_000;

function finalState() {
  return fold(asHarnessEvents(gunzipped(`${RUN}.jsonl.gz`), RUN));
}

describe("the digest is bounded", () => {
  it("keeps the lead prompt of a long hunt under the request ceiling", () => {
    const view = finalState();
    const digest = buildDigest(view, view.hunt.iteration, DEFAULT_DIGEST);
    const rendered = renderDigest(digest);

    expect(RENDERED_MAX).toBeLessThan(DEFAULT_FOLD.max_chars);
    expect(rendered.length).toBeLessThan(RENDERED_MAX);
    expect(digest.recent_evidence.length).toBeLessThanOrEqual(DEFAULT_DIGEST.evidence_max);
    expect(digest.open_questions.length).toBeLessThanOrEqual(DEFAULT_DIGEST.questions_max);
    expect(digest.omitted.evidence_ids.length).toBeLessThanOrEqual(DEFAULT_DIGEST.omitted_ids_max);
  });

  it("selects the same records on every build", () => {
    const first = buildDigest(finalState(), finalState().hunt.iteration, DEFAULT_DIGEST);
    const second = buildDigest(finalState(), finalState().hunt.iteration, DEFAULT_DIGEST);

    expect(second.recent_evidence.map((one) => one.evidence_id)).toEqual(first.recent_evidence.map((one) => one.evidence_id));
    expect(second.omitted).toEqual(first.omitted);
    expect(renderDigest(second)).toBe(renderDigest(first));
  });

  it("keeps the most salient records, and the newest among equals", () => {
    const view = finalState();
    const digest = buildDigest(view, view.hunt.iteration, DEFAULT_DIGEST);
    const unbounded = buildDigest(view, view.hunt.iteration, digestOf(view.hunt.spec));
    const rank = { routine: 0, notable: 1, anomalous: 2 } as const;
    // The unbounded digest lists its records in ledger order, so the index is recency.
    const at = new Map(unbounded.recent_evidence.map((one, index) => [one.evidence_id, index]));
    const kept = new Set(digest.recent_evidence.map((one) => one.evidence_id));
    const dropped = unbounded.recent_evidence.filter((one) => !kept.has(one.evidence_id));

    expect(dropped.length).toBeGreaterThan(0);
    // Nothing the bound dropped outranks anything it kept, on salience then recency.
    for (const keep of digest.recent_evidence) {
      for (const drop of dropped) {
        const outranked =
          rank[drop.salience] < rank[keep.salience] ||
          (rank[drop.salience] === rank[keep.salience] && at.get(drop.evidence_id)! < at.get(keep.evidence_id)!);
        expect(outranked, `${drop.evidence_id} was dropped for ${keep.evidence_id}`).toBe(true);
      }
    }
  });

  it("counts every omitted record and names at most the cap", () => {
    const view = finalState();
    const digest = buildDigest(view, view.hunt.iteration, DEFAULT_DIGEST);

    expect(digest.omitted.count + digest.recent_evidence.length).toBe(view.evidence.size);
    expect(digest.omitted.evidence_ids.length).toBe(Math.min(digest.omitted.count, DEFAULT_DIGEST.omitted_ids_max));
  });

  it("shows the head of the frontier in rank order and says how much is left out", () => {
    const view = finalState();
    const digest = buildDigest(view, view.hunt.iteration, DEFAULT_DIGEST);
    const ranked = rankFrontier(view, view.hunt.iteration).map((one) => one.question);

    expect(digest.open_questions).toEqual(ranked.slice(0, DEFAULT_DIGEST.questions_max));
    expect(digest.notes.join(" ")).toContain(`of ${ranked.length} open questions`);
  });

  // The ledger was written before there was a bound, so it replays under the policy
  // that built its digests: what the fold goldens and replay both rely on.
  it("leaves a ledger written before the bound unbounded on replay", () => {
    const view = finalState();
    expect("evidence_max" in view.hunt.spec.digest).toBe(false);
    expect(digestOf(view.hunt.spec).evidence_max).toBe(Infinity);
    expect(buildDigest(view, view.hunt.iteration, digestOf(view.hunt.spec)).recent_evidence).toHaveLength(144);
  });
});
