// Competing stories: theorize first, then the chosen tests, then a new round.
import { describe, expect, it } from "vitest";
import { options } from "../../../plugins/rootcause/v3/lead.js";
import type { Theory } from "../../../plugins/rootcause/v3/graph.js";
import { snapshot } from "../../../plugins/rootcause/v3/model/snapshot.js";
import { apply, cfg as base, seeded } from "./helpers.js";

const cfg = { ...base, maxTheorize: 2 };
const theory = (id: string, preds: { id: string; next: boolean }[]): Theory => ({
  id, round: 1, story: `story ${id}`, accountsFor: [], gaps: "", status: "live", support: 0, against: 0,
  predictions: preds.map((p) => ({ ...p, sourcetype: "github", expect: `record ${p.id}`, spl: "index=x sourcetype=github", separates: [], status: "open" as const })),
});

describe("stories", () => {
  it("theorize is open until the round cap; a test is open for each unanswered prediction of a live story", () => {
    const g = seeded();
    expect(options(g, cfg).theorize).toEqual(["N1"]);
    expect(options(g, cfg).test).toBeUndefined();
    apply(g, { setTheories: [theory("R1.H1", [{ id: "R1.P1", next: true }, { id: "R1.P2", next: false }]), theory("R1.H2", [{ id: "R1.P3", next: true }])] }, 4);
    expect(options(g, cfg).test).toEqual(["R1.P1", "R1.P2", "R1.P3"]);
    apply(g, { predictionResult: { id: "R1.P1", status: "absent", result: "nothing", toolCallIds: ["r1"], supports: ["R1.H2"], weakens: ["R1.H1"] } }, 5);
    expect(options(g, cfg).test).toEqual(["R1.P2", "R1.P3"]);
    apply(g, { predictionResult: { id: "R1.P3", status: "found", result: "the push", toolCallIds: ["r2"], supports: ["R1.H2"], weakens: [] } }, 6);
    expect(g.theories!.map((t) => [t.id, t.support, t.against])).toEqual([["R1.H1", 0, 1], ["R1.H2", 2, 0]]);
    expect(snapshot(g)).toMatch(/R1.H2 \[\+2 −0\][\s\S]*R1.P3 \[found\] github: record R1.P3 → the push \[r2\]/);
  });
  it("rounds are capped, and a failed round still counts", () => {
    const g = seeded();
    apply(g, { setTheories: [theory("R1.H1", [{ id: "R1.P1", next: true }])] }, 1);
    expect(options(g, cfg).theorize).toEqual(["N1"]);
    apply(g, { setTheories: [] }, 5); // a failed round still counts
    expect(g.theorizeCount).toBe(2);
    expect(g.theories!.every((t) => t.status === "live")).toBe(true);
    expect(options(g, cfg).theorize).toBeUndefined();
  });
});

describe("environment scoping", () => {
  it("maps the environment once, returns it for the run to keep and hands it to the model", async () => {
    const { scope } = await import("../../../plugins/rootcause/v3/scope.js");
    const { MemoryTrace } = await import("../../../plugins/rootcause/v3/trace.js");
    let context = "", turns = 0;
    const model = {
      runTurn: async (req: any) => { turns++; expect(req.move).toBe("scope"); expect(req.userContent).not.toMatch(/graph_snapshot/); return { submission: { map: "**Systems**\n- cluster prod\n\n**Names across sources**\n- node-1 = node-1.internal", reasoning: "" }, toolCalls: [] }; },
      addContext: (t: string) => { context = t; },
    };
    const deps: any = { model, trace: new MemoryTrace("t"), domains: ["a"], sizes: { a: 10 } };
    const held = await scope(deps, base, ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]);
    expect(context).toMatch(/\*\*Names across sources\*\*\n- node-1 = node-1.internal/);
    expect(held).toContain("cluster prod");
    await scope(deps, base, ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"], held ?? undefined); // resume reuses the map
    expect(turns).toBe(1);
  });
});

describe("theorize: nextTests naming", () => {
  const run = async (nextTests: string[], ids: [string, string][]) => {
    const { theorize } = await import("../../../plugins/rootcause/v3/handlers/theorize.js");
    const { MemoryTrace } = await import("../../../plugins/rootcause/v3/trace.js");
    const pred = (id: string) => ({ id, sourcetype: "s", expect: "e", spl: "index=x sourcetype=s", separates: [] });
    const theories = ["H1", "H2", "H3"].map((h) => ({ id: h, story: "s", accountsFor: [], gaps: "", predictions: ids.filter(([t]) => t === h).map(([, p]) => pred(p)) }));
    const model = { runTurn: async () => ({ submission: { theories, nextTests, reasoning: "" }, toolCalls: [] }) };
    const ctx: any = { model, trace: new MemoryTrace("t").forTurn(1, "theorize", "N1"), cfg: base, range: ["a", "b"], domains: ["s"], tools: { coverage: {} }, sizes: {} };
    const patch = await theorize({ move: "theorize", targetId: "N1", turn: 1, reason: "" }, seeded(), ctx);
    return patch.setTheories!.flatMap((t) => t.predictions.filter((p) => p.next).map((p) => p.id));
  };
  const perStory: [string, string][] = [["H1", "P1"], ["H1", "P2"], ["H2", "P1"], ["H3", "P1"]];
  it("maps story-qualified ids when predictions are numbered per story", async () => {
    expect(await run(["H1.P1", "H2-P1"], perStory)).toEqual(["R1.P1", "R1.H2.P1"]);
  });
  it("maps a bare id only when it is unique; falls back to first predictions when nothing matches", async () => {
    expect(await run(["P2"], perStory)).toEqual(["R1.P2"]);
    expect(await run(["P1"], perStory)).toEqual(["R1.P1", "R1.H2.P1", "R1.H3.P1"]); // ambiguous -> fallback
  });
});

describe("theorize: the rare-activity list gets a test", () => {
  it("adds a code-made whole-range prediction when no story tests a rare record", async () => {
    const { theorize } = await import("../../../plugins/rootcause/v3/handlers/theorize.js");
    const { MemoryTrace } = await import("../../../plugins/rootcause/v3/trace.js");
    const g = seeded();
    g.rare = `- edr:process:\n  ParentBaseFileName (39 distinct) [r6]: xz ×5 (2026-03-11 15:54 → 2026-03-11 16:30, node-1)`;
    const theories = ["H1", "H2"].map((h) => ({ id: h, story: "s", accountsFor: [], gaps: "", predictions: [{ id: `${h}P`, sourcetype: "ct", expect: "a CloudTrail call", spl: "index=x sourcetype=ct", separates: [] }] }));
    let sent = "";
    const model = { runTurn: async (req: any) => { sent = req.userContent; return { submission: { theories, nextTests: ["H1P"], reasoning: "" }, toolCalls: [] }; } };
    const ctx: any = { model, trace: new MemoryTrace("t").forTurn(1, "theorize", "N1"), cfg: base, range: ["2026-03-11T00:00:00Z", "2026-03-14T00:00:00Z"], domains: ["ct"], index: "x", tools: { coverage: {} }, sizes: {} };
    const patch = await theorize({ move: "theorize", targetId: "N1", turn: 1, reason: "" }, g, ctx);
    expect(sent).toMatch(/rare-activity list/);
    const p = patch.setTheories![0]!.predictions.find((x) => x.id === "R1.PRARE")!;
    expect(p).toMatchObject({ next: true, sourcetype: "edr:process", earliest: "2026-03-11T00:00:00Z" });
    expect(p.spl).toContain('"xz"');
  });
});
