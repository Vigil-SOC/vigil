// Boundary matching (spec §7.2), pure: does a node found by forward investigation in another domain join
// up with the dead-end node the marker hangs off?
import type { AgentConfig } from "./config.js";
import type { EvidenceDraft, GraphNode } from "./graph.js";
import { MEDIUM_KINDS, STRONG_KINDS, type Artifact } from "./types.js";

// The same event described twice: same source, within 5 seconds, and at least two identifying values in common
// (hosts and accounts alone do not pin an event: they repeat).
const LOOSE = new Set(["host", "user", "process-guid", "parent-process-guid"]);
export function sameEvent(a: Pick<GraphNode, "artifacts" | "observedAt" | "domain">, b: Pick<GraphNode, "artifacts" | "observedAt" | "domain">): boolean {
  if (a.domain !== b.domain || !a.observedAt || !b.observedAt) return false;
  if (Math.abs(Date.parse(a.observedAt) - Date.parse(b.observedAt)) > 5000) return false;
  const shared = a.artifacts.filter((x) => !LOOSE.has(x.kind) && b.artifacts.some((y) => same(x, y)));
  return new Set(shared.map((x) => `${x.kind}|${x.value.toLowerCase()}`)).size >= 2;
}

export interface StitchMatch { kind: "auto" | "tentative" | "none"; joinEvidence: EvidenceDraft[]; reason: string }

const same = (a: Artifact, b: Artifact) => a.kind === b.kind && a.value.trim().toLowerCase() === b.value.trim().toLowerCase();

export function matchBoundary(candidate: Pick<GraphNode, "artifacts" | "observedAt" | "domain" | "label">, marker: Pick<GraphNode, "id">, origin: Pick<GraphNode, "artifacts" | "observedAt" | "label">, cfg: Pick<AgentConfig, "stitchWindowMinutes">, turn: number): StitchMatch {
  const t = (s?: string) => (s ? Date.parse(s) : NaN);
  const ct = t(candidate.observedAt), ot = t(origin.observedAt);
  if (!Number.isNaN(ct) && !Number.isNaN(ot) && ct > ot) return { kind: "none", joinEvidence: [], reason: "candidate is after the origin; causes precede effects" };

  const ev = (summary: string, strength: EvidenceDraft["strength"]): EvidenceDraft => ({ source: candidate.domain, summary: summary.slice(0, 300), strength, polarity: "supports", toolCallIds: [], traceIds: [], turn });
  const joins: EvidenceDraft[] = [];
  const shared = candidate.artifacts.filter((a) => origin.artifacts.some((b) => same(a, b)));
  for (const a of shared) {
    if (STRONG_KINDS.has(a.kind)) joins.push(ev(`stitch ${marker.id}: same ${a.kind} ${a.value} on "${candidate.label}" and "${origin.label}"`, "direct"));
    else if (MEDIUM_KINDS.has(a.kind)) joins.push(ev(`stitch ${marker.id}: same ${a.kind} ${a.value}`, "corroborating"));
  }
  if (!Number.isNaN(ct) && !Number.isNaN(ot) && ot - ct <= cfg.stitchWindowMinutes * 60_000)
    joins.push(ev(`stitch ${marker.id}: candidate at ${candidate.observedAt} is within ${cfg.stitchWindowMinutes} min before origin at ${origin.observedAt}`, "circumstantial"));

  if (joins.some((j) => j.strength === "direct")) return { kind: "auto", joinEvidence: joins, reason: "shared strong artifact" };
  if (joins.length) return { kind: "tentative", joinEvidence: joins, reason: joins.some((j) => j.strength === "corroborating") ? "shared medium artifact" : "timing only" };
  return { kind: "none", joinEvidence: [], reason: "nothing shared" };
}
