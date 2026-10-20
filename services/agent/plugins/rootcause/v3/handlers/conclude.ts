// conclude: the run is ending (budget or turns nearly gone, or nothing left to do). The model writes its own
// verdict from the graph, with no searches; it goes into the report beside the code's.
import type { z } from "zod4";
import { constraints, instructions } from "../model/prompts.js";
import type { SubmitConclusion } from "../model/schemas.js";
import { snapshot } from "../model/snapshot.js";
import { merge, type Handler } from "./common.js";

export const conclude: Handler = async (d, g, ctx) => {
  const userContent = [snapshot(g, 3), instructions.conclude(d.reason), constraints(ctx.cfg, ctx.range, ctx.domains, ctx.domains, "", 0)].join("\n\n");
  const r = await ctx.model.runTurn({ move: "conclude", correlationId: d.targetId, submitTool: "submit_conclusion", allowedDomains: ctx.domains, maxSearches: 0, trace: ctx.trace, userContent });
  const sub = r.submission as z.infer<typeof SubmitConclusion> | null;
  const known = (id: string) => !!(g.nodes[id] || g.edges[id]);
  return merge({ setConclusion: sub
    ? { text: sub.conclusion, rootCause: sub.rootCause && known(sub.rootCause) ? sub.rootCause : undefined, chain: sub.chain.filter(known), openQuestions: sub.openQuestions, turn: ctx.turn }
    : { text: "(the model did not submit a conclusion)", chain: [], openQuestions: [], turn: ctx.turn } });
};
