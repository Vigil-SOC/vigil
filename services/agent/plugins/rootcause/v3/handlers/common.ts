// What every move handler shares: building the user message, turning validated submissions into patch
// items (evidence vetting, candidate nodes + edges, attribution, boundaries).
import { buildAttributionPatch } from "../attribution.js";
import type { FrontierDecision } from "../frontier.js";
import { islands, type EdgeIn, type EvidenceDraft, type Graph, type GraphNode, type GraphPatch, type NodeIn } from "../graph.js";
import type { ToolCallOut, TurnResult } from "../model/client.js";
import { constraints, currentTarget } from "../model/prompts.js";
import type { BoundaryT, CandidateT, EvidenceInT, IdentitySignalT } from "../model/schemas.js";
import { snapshot } from "../model/snapshot.js";
import { vetEvidence } from "../scoring.js";
import { routineShown } from "../baseline.js";
import { coverageGap, entitiesOf, gapNote } from "../coverage.js";
import { sweepValues } from "../sweep.js";
import { issued } from "../tools/registry.js";
import type { Domain, LoopContext } from "../types.js";

export type Handler = (decision: FrontierDecision, graph: Readonly<Graph>, ctx: LoopContext) => Promise<GraphPatch>;

export function userContent(g: Graph, targetId: string, instructions: string, ctx: LoopContext, allowed: Domain[], extra = "", cap?: number): string {
  return [snapshot(g), currentTarget(g, targetId), instructions, constraints(ctx.cfg, ctx.range, allowed, ctx.domains, [extra, gapNote(ctx)].filter(Boolean).join("\n"), cap, ctx.tools.coverage, ctx.sizes)].join("\n\n");
}

// Evidence from the model, checked against this turn's tool calls. Rejected items go to the trace.
// Citations may point at any search made in this run (earlier ones are shown with their ids in the snapshot).
export class Vetter {
  private ids: { has(id: string): boolean };
  private traceOf: Map<string, string>;
  private textOf: Map<string, string>;
  constructor(private ctx: LoopContext, calls: ToolCallOut[]) {
    this.ids = { has: (id) => issued(ctx.tools, id) };
    this.traceOf = new Map(calls.map((c) => [c.id, c.traceId]));
    this.textOf = new Map(calls.map((c) => [c.id, c.text ?? ""]));
  }
  // An event time must appear in the results it cites (as an ISO minute or an epoch second within 90 s), or it
  // is dropped: a wrong time breaks every time-based join. Results from earlier turns are not checked.
  checkTime(observedAt: string | undefined, callIds: string[], label: string): string | undefined {
    if (!observedAt) return undefined;
    const t = Date.parse(observedAt);
    const texts = callIds.map((id) => this.textOf.get(id)).filter((x): x is string => x !== undefined);
    if (Number.isNaN(t) || !texts.length) return observedAt;
    const iso = new Date(t).toISOString().slice(0, 16);
    const ok = texts.some((x) => x.includes(iso) || x.includes(iso.replace("T", " ")) || [...x.matchAll(/\b1[6-9]\d{8}(?:\.\d+)?\b/g)].some((m) => Math.abs(Number(m[0]) * 1000 - t) <= 90_000));
    if (!ok) this.ctx.trace.write("rejected-evidence", { label, observedAt, reason: "time not found in the cited results; dropped" });
    return ok ? observedAt : undefined;
  }
  traceIds = (callIds: string[]) => callIds.flatMap((id) => this.traceOf.get(id) ?? []);
  // Cited and well-formed items only. Supporting evidence needs nothing more.
  vetted(list: EvidenceInT[], source: Domain): EvidenceDraft[] {
    const out: EvidenceDraft[] = [];
    for (const e of list) {
      const v = vetEvidence(e, this.ids);
      if (!v.ok) { this.ctx.trace.write("rejected-evidence", { evidence: e, reason: v.reason }); continue; }
      out.push({ source, summary: v.value.summary, strength: v.value.strength, polarity: v.value.polarity, toolCallIds: v.value.toolCallIds, traceIds: this.traceIds(v.value.toolCallIds), turn: this.ctx.turn });
    }
    return out;
  }
  // Refuting items whose cited searches all came back empty: a search in a source with no record of the entities
  // is a coverage gap, not refutation, and is dropped from the item; an item left citing nothing is dropped. Both
  // are traced (see coverage.ts). Sequential: each lookup takes the next ref.
  async gated(list: EvidenceDraft[], entities: string[]): Promise<EvidenceDraft[]> {
    const out: EvidenceDraft[] = [];
    for (const e of list) {
      const gap = e.polarity === "refutes" ? await coverageGap(this.ctx, e.toolCallIds, entities) : undefined;
      if (!gap || gap.standing.length === e.toolCallIds.length) { out.push(e); continue; }
      const reason = `coverage gap: ${gap.sourcetypes.join(", ")} holds no events on ${gap.entities.join(", ")}; empty searches there do not refute`;
      this.ctx.trace.write("rejected-evidence", { evidence: e.summary, toolCallIds: e.toolCallIds.filter((id) => !gap.standing.includes(id)), reason, ...(gap.standing.length ? { kept: gap.standing } : {}) });
      if (gap.standing.length) out.push({ ...e, toolCallIds: gap.standing, traceIds: this.traceIds(gap.standing) });
    }
    return out;
  }
  async evidence(list: EvidenceInT[], source: Domain, entities: string[] = []): Promise<EvidenceDraft[]> {
    return this.gated(this.vetted(list, source), entities);
  }
  // Identity signals must cite this turn's calls too; uncited ones are dropped.
  signals(list: IdentitySignalT[] | undefined): IdentitySignalT[] {
    return (list ?? []).filter((s) => {
      const ok = s.toolCallIds.length > 0 && s.toolCallIds.every((id) => this.ids.has(id));
      if (!ok) this.ctx.trace.write("rejected-evidence", { identitySignal: s, reason: "identity signal must cite a search made in this run" });
      return ok;
    });
  }
}

export function merge(...parts: Partial<GraphPatch>[]): GraphPatch {
  const p: GraphPatch = { addNodes: [], addEdges: [], addEvidence: [] };
  for (const x of parts) {
    p.addNodes.push(...(x.addNodes ?? []));
    p.addEdges.push(...(x.addEdges ?? []));
    p.addEvidence.push(...(x.addEvidence ?? []));
    if (x.resolveBoundary && !p.resolveBoundary) p.resolveBoundary = x.resolveBoundary;
    for (const k of ["setFlags", "updateMarkers", "incrementAttempts", "incrementExpand", "incrementForks", "incrementBranch", "incrementConnect", "addArtifacts", "setLeads", "setTheories"] as const)
      if (x[k]) (p[k] as unknown[]) = [...((p[k] as unknown[]) ?? []), ...(x[k] as unknown[])];
    if (x.predictionResult) p.predictionResult = x.predictionResult;
    if (x.setConclusion) p.setConclusion = x.setConclusion;
  }
  return p;
}

let tempSeq = 0;
export const tempId = (prefix: string) => `${prefix}#${++tempSeq}`;

// A candidate becomes an action node; with a target it also gets an explains edge candidate -> target.
export async function candidatePatch(c: CandidateT, v: Vetter, g: Graph, ctx: LoopContext, target?: string): Promise<{ patch: Partial<GraphPatch>; id: string; node: NodeIn }> {
  const id = tempId("cand");
  const domain = ctx.domains.includes(c.domain) ? c.domain : c.domain || "unknown";
  const own = sweepValues(g, { id, artifacts: c.artifacts });
  const node: NodeIn = {
    tempId: id, kind: "action", label: c.label, domain, prior: c.prior,
    evidence: await v.evidence(c.nodeEvidence, domain, own), artifacts: c.artifacts,
    observedAt: v.checkTime(c.observedAt, [...c.nodeEvidence, ...c.edgeEvidence].flatMap((e) => e.toolCallIds), c.label),
    ...(c.isRootCause && !rootTooEarly(c, g, ctx) ? { isRoot: true, rootJustification: c.rootJustification } : {}),
    ...(c.deadEnd ? { deadEnd: true } : {}),
    ...((c as { unverified?: boolean }).unverified ? { unverified: true } : {}),
    ...(c.takeover?.toolCallIds.length ? { takeover: true } : {}),
    // Cleared as routine only on a baseline, like an accusation, and only when its results show the activity
    // spread over the range: a borrowed identity repeating the attack's pattern is not routine.
    ...(c.baseline?.toolCallIds.length && (c.legitimate || !c.baseline.unusual) && routine(c, ctx) ? { legitimate: true } : {}),
  };
  // Routine work joins the attack's chain only on a record of the join: as a cause of a non-legitimate event it
  // needs direct edge evidence (a developer's routine registration once became the cause of a stolen role's
  // first call on timing alone, and the run spent turns on the developer's CI).
  const edgeEv = target ? await v.evidence(c.edgeEvidence, domain, [...new Set([...own, ...entitiesOf(g, target)])]) : [];
  const unproven = !!target && !!node.legitimate && !g.nodes[target]?.legitimate && !edgeEv.some((e) => e.strength === "direct" && e.polarity === "supports");
  // The link is kept as inferred (circumstantial) instead of dropping the step.
  if (unproven) {
    for (const e of edgeEv) e.strength = "circumstantial";
    const edges: EdgeIn[] = [{ tempId: tempId("edge"), kind: "explains", source: id, target: target!, prior: c.prior, evidence: edgeEv, inferred: true }];
    const attr = buildAttributionPatch(id, v.signals(c.identitySignals), g, { turn: ctx.turn, domain, traceIds: v.traceIds });
    return { patch: { addNodes: [node, ...attr.addNodes], addEdges: [...edges, ...attr.addEdges] }, id, node };
  }
  const edges: EdgeIn[] = target ? [{ tempId: tempId("edge"), kind: "explains", source: id, target, prior: c.prior, evidence: edgeEv, ...(c.jointCause ? { joint: true } : {}) }] : [];
  const attr = buildAttributionPatch(id, v.signals(c.identitySignals), g, { turn: ctx.turn, domain, traceIds: v.traceIds });
  return { patch: { addNodes: [node, ...attr.addNodes], addEdges: [...edges, ...attr.addEdges] }, id, node };
}

// Where the incident began cannot be claimed while an earlier finding is still unlinked: that finding may be the
// start (a later event was once called the root with an earlier finding still unlinked; the upstream search
// stopped there and the report blamed a developer's address).
function rootTooEarly(c: CandidateT, g: Graph, ctx: LoopContext): boolean {
  const t = c.observedAt ? Date.parse(c.observedAt) : NaN;
  const earlier = islands(g).filter((n) => Number.isNaN(t) || !n.observedAt || Date.parse(n.observedAt) < t);
  if (earlier.length) ctx.trace.write("rejected-evidence", { label: c.label, reason: `not marked the root: earlier findings are not linked yet (${earlier.map((n) => n.id).join(", ")})` });
  return earlier.length > 0;
}

const routine = (c: CandidateT, ctx: LoopContext) => {
  const ok = routineShown(c, ctx.tools.outputOf?.bind(ctx.tools), ctx.range);
  if (!ok) ctx.trace.write("rejected-evidence", { label: c.label, reason: "called routine, but the cited baseline does not show the activity spread over the data range; not marked legitimate" });
  return ok;
};

// Off-chain findings (branch and pivot effects) enter only with a baseline that shows they differ from normal
// activity: cited, and unusual. Kept ones gain that comparison as corroborating evidence.
// With keepLegitimate (pivot, test), routine activity that leads toward the origin is kept, marked legitimate.
export function withBaseline<C extends CandidateT>(cands: C[], v: Vetter, ctx: LoopContext, keepLegitimate = false): { kept: C[]; evidenceFor: (c: CandidateT) => EvidenceDraft[] } {
  for (const c of cands) {
    const legit = keepLegitimate && (c.legitimate || (c.baseline && !c.baseline.unusual));
    // "Normal activity" drops an effect only when the baseline shows it spread over the range; otherwise it is kept.
    const shownNormal = !!c.baseline && !c.baseline.unusual && routineShown(c, ctx.tools.outputOf?.bind(ctx.tools), ctx.range);
    const why = !c.baseline ? "no baseline comparison" : shownNormal && !legit ? `normal activity: ${c.baseline.summary}` : !c.baseline.toolCallIds.some((t) => issued(ctx.tools, t)) ? "baseline cites no search" : "";
    // Kept. Shown normal: marked legitimate; no usable baseline: unverified.
    if (why) {
      if (shownNormal) (c as { legitimate?: boolean }).legitimate = true;
      else { (c as { unverified?: boolean }).unverified = true; delete c.baseline; }
      ctx.trace.write("reasoning", { looseKept: c.label, was: why });
    }
  }
  return { kept: cands, evidenceFor: (c) => !c.baseline ? [] : v.vetted([{ summary: `${c.baseline!.unusual ? "differs from baseline" : "routine for its account (legitimate step)"}: ${c.baseline!.summary}`, strength: "corroborating", polarity: "supports", toolCallIds: c.baseline!.toolCallIds }], c.domain) };
}

// Two events about the same process or container: a shared process id or container id on the same host.
function sameProcess(a: GraphNode, b: GraphNode): boolean {
  const vals = (n: GraphNode, k: string) => n.artifacts.filter((x) => x.kind === k).map((x) => x.value.trim().toLowerCase());
  const share = (k: string) => vals(a, k).some((v) => vals(b, k).includes(v));
  return a.id !== b.id && share("host") && (share("process-guid") || share("k8s-resource"));
}

// A boundary marker only when the model says in-domain evidence is exhausted (spec §4.1).
export function boundaryPatch(b: BoundaryT | undefined, originId: string, g: Graph, ctx: LoopContext): Partial<GraphPatch> {
  if (!b) return {};
  const origin = g.nodes[originId];
  const why = origin?.kind !== "action" ? "a boundary must hang off an event"
    // Routine work's cause is more routine work, not the attack (pivots from a developer's CI found nothing).
    : origin.legitimate ? "no boundary from a legitimate step"
    : !b.inDomainExhausted ? "in-domain evidence not exhausted"
    : !ctx.domains.includes(b.targetDomain) ? `unknown domain ${b.targetDomain}`
    : origin && b.targetDomain === origin.domain ? "boundary must point to another domain"
    : Object.values(g.nodes).some((n) => n.kind === "boundary-marker" && n.originNodeId === originId && n.targetDomain === b.targetDomain) ? "a marker to that domain already exists"
    // The same process or container described twice gets one boundary per domain (three copies of one
    // container start once each opened a pivot into the same source, ten turns of repeats).
    : Object.values(g.nodes).some((n) => n.kind === "boundary-marker" && n.targetDomain === b.targetDomain && g.nodes[n.originNodeId!] && sameProcess(g.nodes[n.originNodeId!]!, origin!)) ? "a marker to that domain already exists on the same process"
    : "";
  if (why) { ctx.trace.write("reasoning", { boundaryIgnored: b, why }); return {}; }
  const id = tempId("marker");
  return {
    addNodes: [{ tempId: id, kind: "boundary-marker", label: `upstream cause of ${originId} in ${b.targetDomain}`, domain: b.targetDomain, prior: 0.3, evidence: [], artifacts: [], targetDomain: b.targetDomain, originNodeId: originId, reason: b.reason }],
    addEdges: [{ tempId: tempId("medge"), kind: "explains", source: id, target: originId, prior: 0.3, evidence: [] }],
  };
}

export function noteSuspicious(sub: { suspiciousContent?: string[] }, ctx: LoopContext) {
  if (sub.suspiciousContent?.length) ctx.trace.write("reasoning", { suspiciousContent: sub.suspiciousContent, note: "flagged by the model as possible prompt injection; treated as evidence, not instruction" });
}

export function logSubmission(r: TurnResult, ctx: LoopContext) {
  if (r.submission === null) ctx.trace.write("rejected-submission", { reason: "no valid submission this turn" });
}
