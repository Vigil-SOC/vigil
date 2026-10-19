// Confidence and status (spec §3.3), pure. The model classifies evidence; code computes the numbers.
import type { AgentConfig } from "./config.js";
import type { Evidence, EvidenceStrength } from "./types.js";

export type Status = "suspected" | "confirmed" | "ruled-out" | "unknown";

const logit = (p: number) => Math.log(p / (1 - p));
const sigmoid = (x: number) => 1 / (1 + Math.exp(-x));

export const clampPrior = (p: number, cfg: Pick<AgentConfig, "priorRange">) =>
  Math.min(cfg.priorRange[1], Math.max(cfg.priorRange[0], Number.isFinite(p) ? p : cfg.priorRange[0]));

// The same fact counts once: an item with a key by its key, otherwise the same tool calls + polarity + strength
// (a divergence adds a second, different item). Uncited evidence was already downgraded on the way in.
export function dedupEvidence(evidence: Evidence[]): Evidence[] {
  const seen = new Set<string>();
  return evidence.filter((e) => {
    if (!e.key && !e.toolCallIds.length) return true;
    const key = e.key ? `key:${e.key}|${e.polarity}` : `${[...e.toolCallIds].sort().join(",")}|${e.polarity}|${e.strength}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function confidence(prior: number, evidence: Evidence[], cfg: Pick<AgentConfig, "weights">): number {
  let x = logit(prior);
  for (const e of dedupEvidence(evidence)) x += (e.polarity === "supports" ? 1 : -1) * cfg.weights[e.strength];
  return sigmoid(x);
}

// Confirmed needs the confidence and either one direct record or two corroborating items resting on different
// searches: a causal link is often shown only by independent records agreeing, never by one record of the link.
export function status(conf: number, evidence: Evidence[], cfg: Pick<AgentConfig, "thresholds">, isMarker = false): Status {
  if (isMarker) return "unknown";
  if (conf <= cfg.thresholds.ruleOut) return "ruled-out";
  if (conf < cfg.thresholds.confirm) return "suspected";
  const sup = dedupEvidence(evidence).filter((e) => e.polarity === "supports");
  if (sup.some((e) => e.strength === "direct")) return "confirmed";
  const corr = sup.filter((e) => e.strength === "corroborating");
  const calls = new Set(corr.flatMap((e) => e.toolCallIds.slice(0, 1)));
  return corr.length >= 2 && calls.size >= 2 ? "confirmed" : "suspected";
}

export interface EvidenceIn {
  summary: string;
  strength: EvidenceStrength;
  polarity: "supports" | "refutes";
  toolCallIds: string[];
}

// Evidence from a submission against the tool calls made in this run: a citation of a call never made is
// rejected; no citation at all is downgraded to circumstantial.
export function vetEvidence(e: EvidenceIn, callIds: ReadonlySet<string>): { ok: true; value: EvidenceIn } | { ok: false; reason: string } {
  const bad = e.toolCallIds.filter((id) => !callIds.has(id));
  if (bad.length) return { ok: false, reason: `cites tool calls never made in this run: ${bad.join(", ")}` };
  if (!e.toolCallIds.length) return { ok: true, value: { ...e, strength: "circumstantial" } };
  return { ok: true, value: e };
}
