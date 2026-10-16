// expand: propose the first causes of a node (spec §4). fork shares the code with a list of exclusions.
// Causes are looked for 48h back by default; a later attempt searches the whole data range. When an attempt
// finds no cause, code sweeps the node's identifying values across every other source (sweep.ts): the hits
// become leads for the next attempt and a boundary into the source that matched most.
import type { SubmitCauses } from "../model/schemas.js";
import { instructions } from "../model/prompts.js";
import type { z } from "zod4";
import { sweep } from "../sweep.js";
import { boundaryPatch, candidatePatch, merge, noteSuspicious, userContent, Vetter, type Handler } from "./common.js";

const CAUSE_LOOKBACK_MS = 48 * 3600_000;

const causes = (fork: boolean): Handler => async (d, g, ctx) => {
  const id = d.targetId;
  const n = g.nodes[id];
  const wide = !fork && n.expandAttempts >= 1;
  const extra = wide ? "\nAn earlier attempt found no cause near the event. Search the whole data range, every sourcetype, starting with the cross-source leads on the current target." : "";
  const r = await ctx.model.runTurn({
    move: d.move, correlationId: id, submitTool: "submit_causes", allowedDomains: ctx.domains, trace: ctx.trace,
    focusTime: wide ? undefined : n.observedAt, beforeMs: CAUSE_LOOKBACK_MS,
    userContent: userContent(g, id, (fork ? instructions.fork(g, id) : instructions.expand(g, id)) + extra, ctx, ctx.domains),
  });
  const bump = fork ? { incrementForks: [id] } : { incrementExpand: [id] };
  const sub = r.submission as z.infer<typeof SubmitCauses> | null;
  if (sub) noteSuspicious(sub, ctx);
  const v = new Vetter(ctx, r.toolCalls);
  const parts = (sub?.candidates ?? []).map((c) => candidatePatch(c, v, g, ctx, id).patch);
  const boundary = boundaryPatch(sub?.boundary, id, g, ctx);

  // Nothing found and no boundary proposed: sweep once, in code.
  if (!parts.length && !boundary.addNodes?.length && !n.swept && n.kind === "action") {
    const s = await sweep(g, n, ctx);
    ctx.trace.write("reasoning", { sweep: id, leads: s.leads, topDomain: s.topDomain });
    const toward = s.topDomain && s.topDomain !== n.domain
      ? boundaryPatch({ targetDomain: s.topDomain, reason: `cross-source sweep: ${s.leads.slice(0, 250)}`, inDomainExhausted: true }, id, g, ctx) : {};
    return merge(bump, { setLeads: [{ nodeId: id, leads: s.leads }] }, toward);
  }
  return merge(bump, ...parts, boundary);
};

export const expand = causes(false);
export const fork = causes(true);
