// strengthen: gather more evidence on one node or edge; its confidence moves up or down (spec §4).
// On an attributed-to edge the identity signals settle who performed the action.
import { buildAttributionPatch } from "../attribution.js";
import type { GraphPatch } from "../graph.js";
import { entitiesOf } from "../coverage.js";
import { instructions } from "../model/prompts.js";
import type { SubmitAssessment } from "../model/schemas.js";
import { boundaryPatch, candidatePatch, merge, noteSuspicious, userContent, Vetter, type Handler } from "./common.js";

export const strengthen: Handler = async (d, g, ctx) => {
  const id = d.targetId;
  const edge = g.edges[id];
  const node = g.nodes[id] ?? g.nodes[edge!.target]!;
  const r = await ctx.model.runTurn({
    move: "strengthen", correlationId: id, submitTool: "submit_assessment", allowedDomains: ctx.domains, focusTime: node.observedAt, trace: ctx.trace,
    maxSearches: ctx.cfg.strengthenSearches,
    userContent: userContent(g, id, instructions.strengthen(g, id), ctx, ctx.domains, "", ctx.cfg.strengthenSearches),
  });
  const bump = { incrementAttempts: [id] };
  const sub = r.submission as SubmitAssessment | null;
  if (!sub) return merge(bump);
  noteSuspicious(sub, ctx);
  const v = new Vetter(ctx, r.toolCalls);
  // The alert is checked for whether it happened, not whether it was benign: that is what the causes decide
  // (a run once ruled the alert out in 2 turns as "routine for its role" before any cause search). On the
  // symptom only a direct record refutes, and it cannot be flagged legitimate.
  const symptom = !edge && !!g.nodes[id]?.isSymptom;
  const all = await v.evidence(sub.evidence, edge ? g.nodes[edge.source]!.domain : node.domain, entitiesOf(g, id));
  const kept = symptom ? all.filter((e) => e.polarity === "supports" || e.strength === "direct") : all;
  if (kept.length < all.length) ctx.trace.write("rejected-evidence", { target: id, dropped: all.length - kept.length, reason: "only a direct record refutes the alert itself" });
  const evidence = kept.map((e) => ({ targetId: id, evidence: e }));
  // Who did it: for a node, the node itself; for an edge, the action it points at.
  const action = edge ? edge.target : id;
  const attr = g.nodes[action]?.kind === "action" ? buildAttributionPatch(action, v.signals(sub.identitySignals), g, { turn: ctx.turn, domain: g.nodes[action].domain, traceIds: v.traceIds }) : {};
  const arts = !edge && sub.artifacts?.length ? { addArtifacts: [{ nodeId: id, artifacts: sub.artifacts }] } : {};
  const legit = !edge && !symptom && sub.legitimate !== undefined ? { setFlags: [{ nodeId: id, legitimate: sub.legitimate }] } : {};
  // Causes found on the way: of the node, or of an edge's source. Never for an attribution edge.
  const causeOf = edge ? (edge.kind === "explains" ? edge.source : undefined) : id;
  const causes: Partial<GraphPatch>[] = [];
  if (causeOf) for (const c of sub.causes ?? []) causes.push((await candidatePatch(c, v, g, ctx, causeOf)).patch);
  // A boundary hangs off an event: never off an actor (an attribution check).
  const origin = edge ? (edge.kind === "explains" ? edge.source : undefined) : g.nodes[id]?.kind === "action" ? id : undefined;
  return merge(bump, { addEvidence: evidence }, attr, arts, legit, ...causes, origin ? boundaryPatch(sub.boundary, origin, g, ctx) : {});
};
