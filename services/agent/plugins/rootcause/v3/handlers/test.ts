// test: run one prediction's search; the outcome scores the stories, the events it turns up enter the graph.
// An event linked as a cause joins like an expand candidate; an effect or an unlinked event needs a baseline,
// like a branch effect.
import { entitiesOf, gapNote } from "../coverage.js";
import type { EvidenceDraft, GraphPatch } from "../graph.js";
import { constraints, instructions } from "../model/prompts.js";
import { snapshot } from "../model/snapshot.js";
import type { SubmitTest } from "../model/schemas.js";
import { wideSearch } from "../baseline.js";
import { issued } from "../tools/registry.js";
import { candidatePatch, merge, noteSuspicious, tempId, Vetter, withBaseline, type Handler } from "./common.js";

export const test: Handler = async (d, g, ctx) => {
  const id = d.targetId;
  const body = [snapshot(g), instructions.test(g, id), constraints(ctx.cfg, ctx.range, ctx.domains, ctx.domains, gapNote(ctx), ctx.cfg.strengthenSearches, ctx.tools.coverage, ctx.sizes)].join("\n\n");
  const r = await ctx.model.runTurn({ move: "test", correlationId: id, submitTool: "submit_test", allowedDomains: ctx.domains, maxSearches: ctx.cfg.strengthenSearches, trace: ctx.trace, userContent: body });
  const sub = r.submission as SubmitTest | null;
  if (!sub) return merge({ predictionResult: { id, status: "unclear", result: "no valid report", toolCallIds: [], supports: [], weakens: [] } });
  noteSuspicious(sub, ctx);
  const v = new Vetter(ctx, r.toolCalls);
  const known = (x?: string) => (x && g.nodes[x]?.kind === "action" ? x : undefined);
  const causes = sub.events.filter((e) => known(e.causeOf));
  const { kept, evidenceFor } = withBaseline(sub.events.filter((e) => !known(e.causeOf)), v, ctx, true);
  const parts: Partial<GraphPatch>[] = [];
  for (const e of causes) parts.push((await candidatePatch(e, v, g, ctx, e.causeOf)).patch);
  for (const e of kept) {
    const x = await candidatePatch(e, v, g, ctx);
    x.node.evidence.push(...evidenceFor(e));
    const from = known(e.effectOf);
    if (from) x.patch.addEdges!.unshift({ tempId: tempId("test"), kind: "explains", source: from, target: x.id, prior: e.prior, evidence: await v.evidence(e.edgeEvidence, e.domain, entitiesOf(g, from)) });
    parts.push(x.patch);
  }
  const ids = sub.toolCallIds.filter((t) => issued(ctx.tools, t));
  // "Absent" only when a cited search covered the whole data range; a narrow window that found nothing settles
  // nothing (EDR "absents" once all searched an hour near the alert, and the attack was two days earlier).
  const narrow = sub.outcome === "absent" && !!ctx.tools.inputOf && !ids.some((t) => wideSearch(ctx.tools.inputOf!(t), ctx.range));
  if (narrow) ctx.trace.write("reasoning", { test: id, downgraded: "absent -> unclear: no cited search covered the whole data range" });
  const status = narrow ? "unclear" : sub.outcome;
  const result = narrow ? `(not settled: searched only part of the data range) ${sub.summary}` : sub.summary;
  // A result that shows a graph event or link did not happen as the graph says refutes it there, so the frontier
  // rechecks it (a pod a test showed "stayed Pending, never ran" once kept its link at 0.76, and the report blamed
  // a developer's address).
  const refuted = (sub.contradicts ?? []).filter((x) => (g.nodes[x]?.kind === "action" && !g.nodes[x].isSymptom) || g.edges[x]?.kind === "explains");
  const addEvidence: { targetId: string; evidence: EvidenceDraft }[] = [];
  if (ids.length) for (const x of refuted) {
    const draft: EvidenceDraft = { source: g.nodes[x]?.domain ?? g.nodes[g.edges[x]!.target]!.domain, summary: `test ${id}: ${sub.summary}`.slice(0, 300), strength: "corroborating", polarity: "refutes", toolCallIds: ids, traceIds: v.traceIds(ids), turn: ctx.turn };
    addEvidence.push(...(await v.gated([draft], entitiesOf(g, x))).map((evidence) => ({ targetId: x, evidence })));
  }
  if (refuted.length) ctx.trace.write("reasoning", { test: id, contradicts: refuted });
  return merge({ predictionResult: { id, status, result, toolCallIds: ids, supports: sub.supports, weakens: narrow ? [] : sub.weakens }, addEvidence }, ...parts);
};
