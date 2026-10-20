// theorize: competing stories of the whole incident, each with predictions a search can check (no searches in
// this move). Ids are prefixed with the round, so earlier rounds' tested predictions stay readable.
import type { Theory } from "../graph.js";
import { constraints, instructions } from "../model/prompts.js";
import type { SubmitTheories } from "../model/schemas.js";
import { snapshot } from "../model/snapshot.js";
import { mentionsRare, rareValues } from "../rare.js";
import { noteSuspicious, type Handler } from "./common.js";

export const theorize: Handler = async (d, g, ctx) => {
  const round = (g.theorizeCount ?? 0) + 1;
  const userContent = [snapshot(g), instructions.theorize(round > 1), constraints(ctx.cfg, ctx.range, ctx.domains, ctx.domains, "", 0, ctx.tools.coverage, ctx.sizes)].join("\n\n");
  // Until a prediction naming a rare-activity record has been tested, each round tests one: stories have
  // cited the rare cluster while the tests stayed near the alert (12 of 15 came back absent).
  const rare = rareValues(g.rare);
  const names = rare.map((x) => x.value);
  const tested = (g.theories ?? []).flatMap((t) => t.predictions).filter((p) => p.status !== "open");
  const requireRare = rare.length && !tested.some((p) => mentionsRare(`${p.spl} ${p.expect}`, names));
  const userContent2 = requireRare ? userContent.replace("</move_instructions>", "At least one prediction must test a record from the rare-activity list in the system context, over the whole data range (earliest/latest = the data range), and be named in nextTests.\n</move_instructions>") : userContent;
  const r = await ctx.model.runTurn({ move: "theorize", correlationId: d.targetId, submitTool: "submit_theories", allowedDomains: ctx.domains, maxSearches: 0, trace: ctx.trace, userContent: userContent2 });
  const sub = r.submission as SubmitTheories | null;
  if (!sub) return { addNodes: [], addEdges: [], addEvidence: [], setTheories: [] };
  noteSuspicious(sub, ctx);
  const R = (id: string) => `R${round}.${id}`;
  // nextTests names a prediction by its own id ("P3") or, when the model numbers them per story, by story and
  // prediction ("H1.P1", "H1-P1"). A bare id shared by several stories means none of them. (Two rounds once
  // named "H1.P1"…; nothing matched, no test ran, and the right story was replaced untested.)
  const key = (x: string) => x.toUpperCase().replace(/[^A-Z0-9]+/g, ".");
  const next = new Set(sub.nextTests.map(key));
  const count = new Map<string, number>();
  for (const t of sub.theories) for (const p of t.predictions) count.set(key(p.id), (count.get(key(p.id)) ?? 0) + 1);
  const isNext = (t: { id: string }, p: { id: string }) => next.has(key(`${t.id}.${p.id}`)) || (count.get(key(p.id)) === 1 && next.has(key(p.id)));
  const theories: Theory[] = sub.theories.map((t) => ({
    id: R(t.id), round, story: t.story, accountsFor: t.accountsFor.filter((id) => g.nodes[id]), gaps: t.gaps, status: "live", support: 0, against: 0,
    predictions: t.predictions.map((p) => ({ ...p, id: R(p.id), separates: p.separates.map(R), next: isNext(t, p), status: "open" as const })),
  }));
  // A rare-activity prediction is tested in this round even if the model did not pick or write one: its own if
  // it wrote one, otherwise one made by code for the rarest record not tested yet.
  if (requireRare && theories.length) {
    const all = theories.flatMap((t) => t.predictions);
    const hit = all.find((p) => mentionsRare(`${p.spl} ${p.expect}`, names));
    if (hit) { if (!all.some((p) => p.next && mentionsRare(`${p.spl} ${p.expect}`, names))) hit.next = true; }
    else {
      const pick = rare.find((x) => !tested.some((p) => mentionsRare(`${p.spl} ${p.expect}`, [x.value]))) ?? rare[0]!;
      const q = (v: string) => `"${v.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
      theories[0]!.predictions.push({ id: R("PRARE"), sourcetype: pick.sourcetype, expect: `the ${pick.count} record(s) where ${pick.field} is ${pick.value.slice(0, 120)}: what they are, which host and account, and whether they belong to the incident`, spl: `index=${ctx.index} sourcetype=${q(pick.sourcetype)} ${q(pick.value)} | stats count min(_time) max(_time) values(*) by sourcetype`, earliest: ctx.range[0], latest: ctx.range[1], separates: [], next: true, status: "open" });
      ctx.trace.write("reasoning", { theorize: "no prediction tested the rare-activity list; added one by code", rare: pick });
    }
  }
  // Nothing matched: test the first prediction of the first stories rather than none.
  if (!theories.some((t) => t.predictions.some((p) => p.next))) {
    ctx.trace.write("reasoning", { nextTestsUnmatched: sub.nextTests, fallback: "first prediction of the first 3 stories" });
    for (const t of theories.slice(0, 3)) t.predictions[0]!.next = true;
  }
  // Prediction ids must be unique in the round (the model may number them per story).
  const seen = new Set<string>();
  for (const t of theories) for (const p of t.predictions) { if (seen.has(p.id)) p.id = `${t.id}.${p.id.split(".").pop()}`; seen.add(p.id); }
  return { addNodes: [], addEdges: [], addEvidence: [], setTheories: theories };
};
