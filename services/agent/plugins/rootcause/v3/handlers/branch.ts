// branch: what else did a solid event produce? Effects become events with an explains edge event -> effect,
// so the incident's other steps (credential theft, lateral movement, data theft) enter the graph even though
// they are not causes of the alert.
import type { z } from "zod4";
import { instructions } from "../model/prompts.js";
import type { SubmitEffects } from "../model/schemas.js";
import { candidatePatch, merge, noteSuspicious, tempId, userContent, Vetter, withBaseline, type Handler } from "./common.js";

export const branch: Handler = async (d, g, ctx) => {
  const id = d.targetId;
  const r = await ctx.model.runTurn({
    move: "branch", correlationId: id, submitTool: "submit_effects", allowedDomains: ctx.domains, trace: ctx.trace,
    userContent: userContent(g, id, instructions.branch(g, id), ctx, ctx.domains),
  });
  const bump = { incrementBranch: [id] };
  const sub = r.submission as z.infer<typeof SubmitEffects> | null;
  if (!sub?.effects.length) return merge(bump);
  noteSuspicious(sub, ctx);
  const v = new Vetter(ctx, r.toolCalls);
  const { kept, evidenceFor } = withBaseline(sub.effects, v, ctx);
  const made = kept.map((c) => {
    const x = candidatePatch(c, v, g, ctx);
    x.node.evidence.push(...evidenceFor(c));
    x.patch.addEdges!.unshift({ tempId: tempId("branch"), kind: "explains", source: id, target: x.id, prior: c.prior, evidence: v.evidence(c.edgeEvidence, c.domain) });
    return x.patch;
  });
  return merge(bump, ...made);
};
