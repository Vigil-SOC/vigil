// choose: the model names the next move. No searches and no patch; the orchestrator reads the choice off the
// turn, checks it against the graph like any submission's `next`, and runs it the next turn.
import { constraints, instructions } from "../model/prompts.js";
import { snapshot } from "../model/snapshot.js";
import { merge, type Handler } from "./common.js";

export const choose: Handler = async (d, g, ctx) => {
  const userContent = [snapshot(g, 3), instructions.choose(), constraints(ctx.cfg, ctx.range, ctx.domains, ctx.domains, "", 0, ctx.tools.coverage, ctx.sizes)].join("\n\n");
  await ctx.model.runTurn({ move: "choose", correlationId: d.targetId, submitTool: "submit_choice", allowedDomains: ctx.domains, maxSearches: 0, trace: ctx.trace, userContent });
  return merge();
};
