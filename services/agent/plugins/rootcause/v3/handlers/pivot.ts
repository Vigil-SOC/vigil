// pivot (spec §7.1): forward investigation in the boundary's domain, one step per turn. Step 0 finds
// entry events; each later step asks what the best lead produced. Every new node is run through the
// stitch matcher against the marker's origin.
import type { z } from "zod4";
import type { GraphNode, GraphPatch, NodeIn } from "../graph.js";
import { instructions } from "../model/prompts.js";
import type { SubmitEffects, SubmitEntryPoints } from "../model/schemas.js";
import { matchBoundary, sameEvent } from "../stitch.js";
import { candidatePatch, merge, noteSuspicious, tempId, userContent, Vetter, withBaseline, type Handler } from "./common.js";

export const pivot: Handler = async (d, g, ctx) => {
  const m = g.nodes[d.targetId]!;
  const origin = g.nodes[m.originNodeId!]!;
  const steps = m.pivotSteps ?? 0;
  const exhausted = (): GraphPatch => merge({ updateMarkers: [{ markerId: m.id, pivotSteps: ctx.cfg.maxForwardSteps + 1, forwardLeaves: [] }] });

  // Stitch each new node against the origin: auto resolves the marker, tentative adds a suspected edge.
  const stitch = (nodes: NodeIn[]): Partial<GraphPatch> => {
    const out: Partial<GraphPatch> = { addEdges: [] };
    for (const n of nodes) {
      const s = matchBoundary({ ...n, label: n.label }, m, origin, ctx.cfg, ctx.turn);
      ctx.trace.write("stitch", { marker: m.id, candidate: n.label, kind: s.kind, reason: s.reason });
      if (s.kind === "auto" && !out.resolveBoundary) out.resolveBoundary = { markerId: m.id, nodeId: n.tempId, joinEvidence: s.joinEvidence };
      else if (s.kind === "tentative") out.addEdges!.push({ tempId: tempId("stitch"), kind: "explains", source: n.tempId, target: origin.id, prior: 0.3, evidence: s.joinEvidence, stitchFor: m.id });
    }
    return out;
  };

  if (steps === 0 || !m.forwardLeaves?.length) {
    if (steps > 0) return exhausted();
    const end = origin.observedAt ?? ctx.range[1];
    const start = new Date(Math.max(Date.parse(end) - ctx.cfg.lookbackDays * 86_400_000, Date.parse(ctx.range[0]))).toISOString();
    // Model-led: the pivot searches the source the model chose, when it names one.
    const into = d.source && ctx.domains.includes(d.source) ? d.source : m.targetDomain!;
    const allowed = [into];
    const r = await ctx.model.runTurn({
      move: "pivot-entry", correlationId: m.id, submitTool: "submit_entry_points", allowedDomains: allowed, trace: ctx.trace,
      userContent: userContent(g, m.id, instructions.pivotEntry(g, m.id, [start, end], into), ctx, allowed),
    });
    const sub = r.submission as z.infer<typeof SubmitEntryPoints> | null;
    if (!sub?.candidates.length) return exhausted();
    noteSuspicious(sub, ctx);
    const v = new Vetter(ctx, r.toolCalls);
    const { kept, evidenceFor } = withBaseline(sub.candidates, v, ctx, true);
    if (!kept.length) return exhausted();
    const made = kept.map((c) => { const x = candidatePatch(c, v, g, ctx); x.node.evidence.push(...evidenceFor(c)); return x; });
    return merge(...made.map((x) => x.patch), stitch(made.map((x) => x.node)), { updateMarkers: [{ markerId: m.id, pivotSteps: 1, forwardLeaves: made.map((x) => x.id) }] });
  }

  // A lead the pivot already linked to the origin (a stitched link that is now strong) closes the boundary:
  // stepping on from it only re-finds the same events.
  const linked = m.forwardLeaves.map((id) => Object.values(g.edges).find((e) => e.kind === "explains" && e.source === id && e.target === origin.id && !e.retired && e.status !== "ruled-out" && e.confidence >= ctx.cfg.thresholds.expandMin)).find(Boolean);
  if (linked) {
    ctx.trace.write("stitch", { marker: m.id, candidate: linked.source, kind: "linked", reason: `${linked.source} already explains ${origin.id} (${linked.id} ${linked.confidence.toFixed(2)})` });
    return merge({ resolveBoundary: { markerId: m.id, nodeId: linked.source, joinEvidence: [] }, updateMarkers: [{ markerId: m.id, pivotSteps: steps, forwardLeaves: [] }] });
  }

  // Forward only from leads that happened before the origin: past it, the pivot is retelling what followed.
  const originT = origin.observedAt ? Date.parse(origin.observedAt) : Infinity;
  const before = (n: { observedAt?: string }) => !n.observedAt || Date.parse(n.observedAt) <= originT;
  const leaves = m.forwardLeaves.map((id) => g.nodes[id]).filter((n): n is GraphNode => !!n && before(n));
  if (!leaves.length) return exhausted();
  const leaf = [...leaves].sort((a, b) => b.confidence - a.confidence)[0]!;
  const allowed = [...new Set([d.source && ctx.domains.includes(d.source) ? d.source : m.targetDomain!, origin.domain])];
  const r = await ctx.model.runTurn({
    move: "pivot-forward", correlationId: m.id, submitTool: "submit_effects", allowedDomains: allowed, focusTime: leaf.observedAt, trace: ctx.trace,
    userContent: userContent(g, leaf.id, instructions.pivotForward(g, m.id, leaf.id), ctx, allowed),
  });
  const sub = r.submission as z.infer<typeof SubmitEffects> | null;
  const rest = m.forwardLeaves.filter((id) => id !== leaf.id);
  if (!sub?.effects.length) return merge({ updateMarkers: [{ markerId: m.id, pivotSteps: steps + 1, forwardLeaves: rest }] });
  noteSuspicious(sub, ctx);
  const v = new Vetter(ctx, r.toolCalls);
  // An effect that is the origin event itself closes the boundary: the leaf caused the origin.
  const isOrigin = sub.effects.find((c) => sameEvent({ ...c, observedAt: c.observedAt }, origin));
  if (isOrigin) {
    const join = v.evidence([...isOrigin.edgeEvidence, { summary: `the effect of ${leaf.id} is the origin event ${origin.id} itself (same source, time and identifiers)`, strength: "direct", polarity: "supports", toolCallIds: isOrigin.nodeEvidence.flatMap((e) => e.toolCallIds) }], origin.domain);
    ctx.trace.write("stitch", { marker: m.id, candidate: isOrigin.label, kind: "same-event", reason: `effect of ${leaf.id} is ${origin.id}` });
    return merge({ resolveBoundary: { markerId: m.id, nodeId: leaf.id, joinEvidence: join }, updateMarkers: [{ markerId: m.id, pivotSteps: steps + 1, forwardLeaves: [] }] });
  }
  // Effects after the origin are not causes of it; effects need a baseline like any off-chain finding.
  // The leaf reported again as its own effect is no step forward.
  const timely = sub.effects.filter((c) => !sameEvent(c, leaf)).filter((c) => { const ok = before(c); if (!ok) ctx.trace.write("rejected-evidence", { effect: c.label, reason: "after the origin: not a cause, not added" }); return ok; });
  const { kept, evidenceFor } = withBaseline(timely, v, ctx, true);
  // Effect: the leaf explains it (leaf -> effect).
  const made = kept.map((c) => {
    const x = candidatePatch(c, v, g, ctx);
    x.node.evidence.push(...evidenceFor(c));
    x.patch.addEdges!.unshift({ tempId: tempId("fwd"), kind: "explains", source: leaf.id, target: x.id, prior: c.prior, evidence: v.evidence(c.edgeEvidence, c.domain) });
    return x;
  });
  return merge(...made.map((x) => x.patch), stitch(made.map((x) => x.node)), { updateMarkers: [{ markerId: m.id, pivotSteps: steps + 1, forwardLeaves: [...rest, ...made.map((x) => x.id)] }] });
};
