// Findings no link ties to the chain get a connect move; legitimate steps join the attack only on direct
// evidence; no pivots or boundaries from legitimate steps; tests before pivots.
import { describe, expect, it } from "vitest";
import { buildAttributionPatch } from "../../../plugins/rootcause/v3/attribution.js";
import { selectFrontier } from "../../../plugins/rootcause/v3/frontier.js";
import { islands, type Theory } from "../../../plugins/rootcause/v3/graph.js";
import { MemoryTrace } from "../../../plugins/rootcause/v3/trace.js";
import { apply, cfg as base, ev, node, seeded } from "./helpers.js";

const cfg = { ...base, maxTheorize: 2 };
const range: [string, string] = ["2026-03-11T00:00:00Z", "2026-03-14T00:00:00Z"];
const ctxFor = (sub: unknown, callCount = 3): any => ({
  model: { runTurn: async () => ({ submission: sub, toolCalls: [] }) }, trace: new MemoryTrace("t").forTurn(5, "connect", "N2"),
  cfg, range, domains: ["edr", "cloudtrail"], tools: { callCount, coverage: {} }, sizes: {}, turn: 5,
});
const theory: Theory = { id: "R1.H1", round: 1, story: "s", accountsFor: [], gaps: "", status: "live", support: 0, against: 0,
  predictions: [{ id: "R1.P1", sourcetype: "edr", expect: "e", spl: "index=x", separates: [], next: true, status: "open" }] };

// S <- N2 (chain top), plus two unlinked findings N3 (earlier) and N4 (later); a weak one N5 does not count.
function graph() {
  const g = seeded();
  apply(g, { addNodes: [node("top", { prior: 0.5, observedAt: "2026-03-13T12:34:34Z", evidence: [ev("direct", "supports", ["t1"]), ev("direct", "supports", ["t2"])] })], addEdges: [{ tempId: "e", kind: "explains", source: "top", target: "N1", prior: 0.5, evidence: [ev("direct", "supports", ["t3"])] }] });
  apply(g, { addNodes: [
    node("xz", { prior: 0.5, label: "implant loaded", observedAt: "2026-03-11T16:30:00Z", evidence: [ev("direct", "supports", ["r1"]), ev("direct", "supports", ["r2"])] }),
    node("day1", { prior: 0.5, label: "role used a day before", domain: "cloudtrail", observedAt: "2026-03-12T12:46:00Z", evidence: [ev("direct", "supports", ["d1"]), ev("direct", "supports", ["d2"])] }),
    node("weak", { prior: 0.2, label: "maybe something" }),
  ] });
  g.theories = [structuredClone(theory)]; g.theorizeCount = 1; g.theorizedTurn = 5;
  return g;
}

describe("connect", () => {
  it("unlinked findings are islands, latest first; weak, legitimate and chain events are not", () => {
    const g = graph();
    apply(g, { addNodes: [node("dev", { prior: 0.5, legitimate: true, evidence: [ev("direct", "supports", ["v1"]), ev("direct", "supports", ["v2"])] })] });
    expect(islands(g).map((n) => n.id)).toEqual(["N4", "N3"]);
  });
  it("comes before the stories' tests, once per finding", () => {
    const g = graph();
    expect(selectFrontier(g, cfg, 5)).toMatchObject({ move: "connect", targetId: "N2", priority: 1 });
    apply(g, { incrementConnect: ["N3", "N4"] }, 5);
    expect(selectFrontier(g, cfg, 6)?.move).not.toBe("connect");
  });
  it("links findings through a new event and resolves the chain top's open boundary", async () => {
    const { connect } = await import("../../../plugins/rootcause/v3/handlers/connect.js");
    const g = graph();
    apply(g, { addNodes: [{ tempId: "m", kind: "boundary-marker", label: "upstream cause of N2 in edr", domain: "edr", prior: 0.3, evidence: [], artifacts: [], targetDomain: "edr", originNodeId: "N2" }], addEdges: [{ tempId: "me", kind: "explains", source: "m", target: "N2", prior: 0.3, evidence: [] }] });
    const cite = (summary: string) => [{ summary, strength: "direct", polarity: "supports", toolCallIds: ["r1"] }];
    const sub = {
      events: [{ ref: "X1", label: "token file read on the node", domain: "edr", prior: 0.4, observedAt: "2026-03-11T17:00:00Z", artifacts: [{ kind: "host", value: "node-b" }], nodeEvidence: cite("the read"), edgeEvidence: [] }],
      links: [
        { from: "N3", to: "X1", prior: 0.4, evidence: cite("same host, after the implant") },
        { from: "X1", to: "N4", prior: 0.4, evidence: cite("the token's role is used next day") },
        { from: "N4", to: "N2", prior: 0.4, evidence: cite("same role session and address") },
        { from: "N2", to: "N3", prior: 0.4, evidence: cite("backwards in time") },
      ],
      reasoning: "",
    };
    const patch = await connect({ move: "connect", targetId: "N2", turn: 5, priority: 1, reason: "" }, g, ctxFor(sub));
    expect(patch.incrementConnect).toEqual(["N4", "N3"]);
    expect(patch.resolveBoundary).toMatchObject({ markerId: "B1", nodeId: "N4" });
    const r = apply(g, patch, 5);
    const x1 = r.idMap[patch.addNodes[0]!.tempId];
    const links = Object.values(g.edges).filter((e) => e.kind === "explains" && !e.retired).map((e) => `${e.source}>${e.target}`);
    expect(links).toEqual(expect.arrayContaining(["N3>" + x1, x1 + ">N4", "N4>N2"]));
    expect(links).not.toContain("N2>N3"); // a cause comes first
    expect(g.nodes.B1!.resolvedTo).toBe("N4");
    expect(islands(g)).toEqual([]);
  });
  it("a legitimate step links into the attack as an inferred link unless it has direct evidence", async () => {
    const { connect } = await import("../../../plugins/rootcause/v3/handlers/connect.js");
    const g = graph();
    apply(g, { setFlags: [{ nodeId: "N4", legitimate: true }] });
    const link = (strength: string) => ({ events: [], links: [{ from: "N4", to: "N2", prior: 0.4, evidence: [{ summary: "s", strength, polarity: "supports", toolCallIds: ["r1"] }] }], reasoning: "" });
    const weak = (await connect({ move: "connect", targetId: "N2", turn: 5, priority: 1, reason: "" }, g, ctxFor(link("circumstantial")))).addEdges;
    expect(weak).toHaveLength(1);
    expect(weak![0]).toMatchObject({ inferred: true });
    const strong = (await connect({ move: "connect", targetId: "N2", turn: 5, priority: 1, reason: "" }, g, ctxFor(link("direct")))).addEdges;
    expect(strong).toHaveLength(1);
    expect(strong![0]!.inferred).toBeUndefined();
  });
});

describe("legitimate steps", () => {
  it("a legitimate cause candidate without direct edge evidence joins as an inferred link", async () => {
    const { candidatePatch, Vetter } = await import("../../../plugins/rootcause/v3/handlers/common.js");
    const g = graph();
    const ctx = ctxFor(null);
    ctx.tools.outputOf = () => '{"first":"1773194400","last":"1773450800"}'; // spread over the range: routine
    const c = (strength: "direct" | "circumstantial") => ({ label: "dev registered a runner", domain: "edr", prior: 0.3, artifacts: [], nodeEvidence: [], identitySignals: [],
      edgeEvidence: [{ summary: "s", strength, polarity: "supports" as const, toolCallIds: ["r1"] }], legitimate: true, baseline: { summary: "b", unusual: false, toolCallIds: ["r2"] } });
    const v = new Vetter(ctx, []);
    expect(candidatePatch(c("circumstantial"), v, g, ctx, "N2").patch.addEdges).toMatchObject([{ inferred: true }]);
    const direct = candidatePatch(c("direct"), v, g, ctx, "N2").patch.addEdges;
    expect(direct).toHaveLength(1);
    expect(direct![0]!.inferred).toBeUndefined();
  });
  it("no boundary and no pivot from a legitimate step; the stories' tests go before pivots", async () => {
    const { boundaryPatch } = await import("../../../plugins/rootcause/v3/handlers/common.js");
    const g = graph();
    apply(g, { incrementConnect: ["N3", "N4"], incrementExpand: ["N2"] });
    const ctx = ctxFor(null);
    const b = { targetDomain: "cloudtrail", reason: "r", inDomainExhausted: true };
    apply(g, { setFlags: [{ nodeId: "N2", legitimate: true }] });
    expect(boundaryPatch(b, "N2", g, ctx)).toEqual({});
    apply(g, { setFlags: [{ nodeId: "N2", legitimate: false }] });
    apply(g, boundaryPatch(b, "N2", g, ctx) as any);
    expect(selectFrontier(g, cfg, 6)).toMatchObject({ move: "test", targetId: "R1.P1" });
    apply(g, { predictionResult: { id: "R1.P1", status: "found", result: "x", toolCallIds: [], supports: [], weakens: [] } }, 6);
    expect(selectFrontier(g, cfg, 7)).toMatchObject({ move: "pivot" });
    apply(g, { setFlags: [{ nodeId: "N2", legitimate: true }] });
    expect(selectFrontier(g, cfg, 7)?.move).not.toBe("pivot");
  });
  it("an account that ran a step someone else operated is not listed as an indicator", async () => {
    const { renderReport } = await import("../../../plugins/rootcause/v3/report.js");
    const g = seeded();
    const tctx = { turn: 1, domain: "github", traceIds: () => [] };
    apply(g, { addNodes: [
      node("merge", { label: "dev merged a PR", legitimate: true, prior: 0.5, artifacts: [{ kind: "user", value: "devuser" }], evidence: [ev("direct", "supports", ["a"]), ev("direct", "supports", ["b"])] }),
      node("run", { label: "workflow run on outsider's PR", prior: 0.5, artifacts: [{ kind: "user", value: "devuser" }, { kind: "user", value: "outsider" }], evidence: [ev("direct", "supports", ["c"]), ev("direct", "supports", ["d"])] }),
    ], addEdges: [{ tempId: "l", kind: "explains", source: "run", target: "N1", prior: 0.5, evidence: [ev("direct", "supports", ["e"]), ev("direct", "supports", ["f"])] }] });
    apply(g, buildAttributionPatch("N2", [{ layer: "machine-facing", relation: "ran-as", kind: "actor-login", value: "devuser", resolvesTo: "github:devuser", toolCallIds: ["r1"] }], g, tctx));
    apply(g, buildAttributionPatch("N3", [
      { layer: "machine-facing", relation: "ran-as", kind: "actor-login", value: "devuser", resolvesTo: "github:devuser", toolCallIds: ["r1"] },
      { layer: "human-facing", relation: "operated-by", kind: "actor-login", value: "outsider", resolvesTo: "github:outsider", toolCallIds: ["r2"] },
    ], g, tctx));
    const md = renderReport(g, new MemoryTrace("t"), null, { id: "a", source: "edr", title: "t", observedAt: "2026-04-02T00:00:00Z", artifacts: [], raw: {} });
    expect(md).toMatch(/- user: outsider\n/);
    expect(md).toMatch(/github:devuser: legitimate user/);
  });
});

describe("events must be records", () => {
  it("refuses an event that says it happened outside the logs", async () => {
    const { check, UNSEEN } = await import("../../../plugins/rootcause/v3/model/client.js");
    for (const l of ["svc-deploy per-session STS credential minted outside captured telemetry", "token issued, not recorded in the logs", "key with no visible mint"]) expect(UNSEEN.test(l)).toBe(true);
    for (const l of ["xz downloaded to nodes", "runner job step on node-b", "logs deleted by DeleteRule"]) expect(UNSEEN.test(l)).toBe(false);
    const cand = (label: string) => ({ label, domain: "edr", prior: 0.3, artifacts: [], nodeEvidence: [], edgeEvidence: [] });
    const r = check("submit_causes", { candidates: [cand("credential minted outside captured telemetry"), cand("token file read")], reasoning: "" }, 1);
    expect(r).toMatchObject({ success: false, drop: [["candidates", 0]] });
  });
});

describe("islands", () => {
  it("an effect of a chain event is linked, not an island", () => {
    const g = graph();
    apply(g, { addEdges: [{ tempId: "x", kind: "explains", source: "N2", target: "N4", prior: 0.4, evidence: [] }] });
    expect(islands(g).map((n) => n.id)).toEqual(["N3"]);
  });
});

describe("routine over several days", () => {
  it("two days of activity in a three-day range is not routine, even when a midnight day-bucket stretches the spread", async () => {
    const { routineShown } = await import("../../../plugins/rootcause/v3/baseline.js");
    const c: any = { baseline: { summary: "s", unusual: false, toolCallIds: ["r1", "r2"] } };
    const out: Record<string, string> = { r1: '{"_time":"2026-03-12T00:00","count":"10"}\n{"_time":"2026-03-13T00:00","count":"6"}', r2: '{"t":"1773320442"}\n{"t":"1773405275"}' };
    expect(routineShown(c, (id) => out[id], range)).toBe(false);
    out.r3 = '{"_time":"2026-03-11T00:00"}\n{"_time":"2026-03-12T00:00"}\n{"_time":"2026-03-13T00:00"}';
    expect(routineShown({ baseline: { ...c.baseline, toolCallIds: ["r3", "r2"] } } as any, (id) => out[id], range)).toBe(true);
  });
});

describe("a link needs a value both events carry", () => {
  const on = { ...cfg, linkNeedsSharedValue: true };
  const art = (kind: string, value: string) => ({ kind: kind as any, value });
  it("keeps same host + nearby time across accounts or sources as an inferred link; a shared process, file name or token is a join", async () => {
    const { applyPatch, emptyPatch, createGraph, sharedValue } = await import("../../../plugins/rootcause/v3/graph.js");
    const g = createGraph("t");
    const add = (p: any) => applyPatch(g, { ...emptyPatch(), ...p }, 1, on);
    add({ addNodes: [
      node("logon", { label: "NTLM logon to SRV-01 from 192.0.2.14 with alice's credential", domain: "wineventlog", artifacts: [art("host", "SRV-01"), art("user", "alice"), art("ip", "192.0.2.14")] }),
      node("psexec", { label: "PsExec deploy.bat on SRV-01 by bob", domain: "wineventlog", artifacts: [art("host", "SRV-01"), art("user", "bob")] }),
      node("debug", { label: "debug pod created on ip-192-0-2-30", domain: "eks", artifacts: [art("host", "ip-192-0-2-30.internal"), art("ip", "198.51.100.67")] }),
      node("runc", { label: "runc create container a1b2c3d4 on ip-192-0-2-30", domain: "edr", artifacts: [art("host", "ip-192-0-2-30.internal"), art("k8s-resource", "a1b2c3d4")] }),
      node("mail", { label: "email with attachment Invoice_10001.xlsm", domain: "email", artifacts: [art("domain-name", "bad.example")] }),
      node("excel", { label: "EXCEL opened the attachment", domain: "edr", artifacts: [art("process", '"EXCEL.EXE" C:\\Users\\a\\Downloads\\Invoice_10001.xlsm')] }),
    ] });
    const sig = (id: string, v: string) => buildAttributionPatch(id, [{ layer: "machine-facing", relation: "ran-as", kind: "account", value: v, resolvesTo: `ad:${v}`, toolCallIds: ["r1"] }], g, { turn: 1, domain: "wineventlog", traceIds: () => [] });
    add(sig("N1", "alice")); add(sig("N2", "bob"));
    expect(sharedValue(g, g.nodes.N1!, g.nodes.N2!)).toBeNull(); // host only, different accounts
    expect(sharedValue(g, g.nodes.N3!, g.nodes.N4!)).toBeNull(); // host only, different sources
    expect(sharedValue(g, g.nodes.N5!, g.nodes.N6!)).toMatch(/invoice_10001\.xlsm/i);
    const r = add({ addEdges: [
      { tempId: "x", kind: "explains", source: "N1", target: "N2", prior: 0.4, evidence: [ev("direct")] },
      { tempId: "y", kind: "explains", source: "N5", target: "N6", prior: 0.4, evidence: [ev("direct")] },
    ] });
    expect(r.rejected).toEqual([]);
    const edges = Object.values(g.edges).filter((e) => e.kind === "explains");
    expect(edges.map((e) => `${e.source}>${e.target}:${e.inferred === true}`)).toEqual(["N1>N2:true", "N5>N6:false"]);
    expect(edges[0]!.evidence.every((v) => v.strength === "circumstantial")).toBe(true);
  });
});

describe("contradicting tests and early roots", () => {
  it("a test result that contradicts a link refutes it there", async () => {
    const { test: testHandler } = await import("../../../plugins/rootcause/v3/handlers/test.js");
    const g = graph();
    const sub = { outcome: "unclear", summary: "the pod stayed Pending and never ran", toolCallIds: ["r1"], supports: [], weakens: [], events: [], contradicts: ["E1", "N1", "E99"], reasoning: "" };
    const ctx = ctxFor(sub, 1); ctx.tools.inputOf = () => ({ earliest: range[0], latest: range[1] });
    const patch = await testHandler({ move: "test", targetId: "R1.P1", turn: 5, priority: 1, reason: "" }, g, ctx);
    // E1 is the chain link: refuted; N1 is the symptom (never refuted this way); E99 does not exist.
    expect(patch.addEvidence).toMatchObject([{ targetId: "E1", evidence: { polarity: "refutes", strength: "corroborating" } }]);
  });
  it("the root cannot be claimed while an earlier finding is unlinked", async () => {
    const { candidatePatch, Vetter } = await import("../../../plugins/rootcause/v3/handlers/common.js");
    const g = graph(); // N3 (Mar 11 16:30) is unlinked
    const ctx = ctxFor(null);
    const c = (at: string) => ({ label: "pod created", domain: "edr", prior: 0.3, artifacts: [], nodeEvidence: [], edgeEvidence: [], observedAt: at, isRootCause: true });
    const v = new Vetter(ctx, []);
    expect(candidatePatch(c("2026-03-11T17:00:00Z"), v, g, ctx, "N2").node.isRoot).toBeUndefined();
    expect(candidatePatch(c("2026-03-11T10:00:00Z"), v, g, ctx, "N2").node.isRoot).toBe(true);
  });
});

describe("verdict path, interleaved branch", () => {
  it("a longer chain of solid hops beats a short path that ends at an open boundary", async () => {
    const { allPaths } = await import("../../../plugins/rootcause/v3/graph.js");
    const g = seeded();
    const strong = (t: string) => ({ tempId: t, prior: 0.5, evidence: [ev("direct", "supports", [`${t}1`]), ev("direct", "supports", [`${t}2`])] });
    apply(g, { addNodes: [node("a", strong("a")), node("b", strong("b")), { tempId: "m", kind: "boundary-marker", label: "m", domain: "x", prior: 0.3, evidence: [], artifacts: [], targetDomain: "x", originNodeId: "N2" }],
      addEdges: [{ tempId: "e1", kind: "explains", source: "a", target: "N1", prior: 0.5, evidence: [ev("direct", "supports", ["l1"])] }, { tempId: "e2", kind: "explains", source: "b", target: "a", prior: 0.5, evidence: [ev("direct", "supports", ["l2"])] }, { tempId: "e3", kind: "explains", source: "m", target: "a", prior: 0.3, evidence: [] }] });
    expect(allPaths(g)[0]!.nodeIds).toEqual(["N1", "N2", "N3"]);
  });
  it("branches from the chain event nearest the alert every branchEvery turns once the stories run", () => {
    const g = graph(); // S <- N2 (top), stories since turn 5
    g.storiesSince = 5;
    apply(g, { incrementConnect: ["N3", "N4"], incrementExpand: ["N2"] });
    expect(selectFrontier(g, { ...cfg, branchEvery: 6 }, 8)?.move).toBe("test");
    expect(selectFrontier(g, { ...cfg, branchEvery: 6 }, 11)).toMatchObject({ move: "branch", targetId: "N2" });
    apply(g, { incrementBranch: ["N2"] }, 11);
    expect(selectFrontier(g, { ...cfg, branchEvery: 6 }, 12)?.move).toBe("test");
  });
});

describe("role ARNs: a hand-off joins, two sessions of one role do not", () => {
  it("dev's session of role/admin does not join the attacker's function; create-with-role -> running-as-role does", async () => {
    const { applyPatch, emptyPatch, createGraph, sharedValue } = await import("../../../plugins/rootcause/v3/graph.js");
    const on = { ...cfg, linkNeedsSharedValue: true };
    const g = createGraph("t");
    const add = (p: any) => applyPatch(g, { ...emptyPatch(), ...p }, 1, on);
    const role = { kind: "arn" as const, value: "arn:aws:iam::111122223333:role/admin" };
    add({ addNodes: [
      node("dev", { label: "PutRule rule-a", domain: "cloudtrail", artifacts: [role, { kind: "ip", value: "198.51.100.67" }] }),
      node("create", { label: "CreateFunction fn-sync with execution role admin", domain: "cloudtrail", artifacts: [role] }),
      node("run", { label: "fn-sync running as admin deleted the rule", domain: "cloudtrail", artifacts: [role] }),
    ] });
    const sig = (id: string, v: string) => buildAttributionPatch(id, [{ layer: "machine-facing", relation: "ran-as", kind: "iam-principal", value: v, resolvesTo: `aws:${v}`, toolCallIds: ["r1"] }], g, { turn: 1, domain: "cloudtrail", traceIds: () => [] });
    add(sig("N1", "arn:aws:sts::111122223333:assumed-role/admin/session-a"));
    add(sig("N2", "arn:aws:sts::111122223333:assumed-role/ops-controller/svc-deploy"));
    add(sig("N3", "arn:aws:sts::111122223333:assumed-role/admin/fn-sync"));
    expect(sharedValue(g, g.nodes.N1!, g.nodes.N3!)).toBeNull(); // two sessions of role/admin
    expect(sharedValue(g, g.nodes.N1!, g.nodes.N2!)).toBeNull(); // the dev ran as it, the create only names it: not a hand-off this way
    expect(sharedValue(g, g.nodes.N2!, g.nodes.N3!)).toMatch(/role\/admin/); // created with the role -> runs as it
  });
});

describe("routine work leads to more routine work", () => {
  it("a step causing its own account's legitimate step is legitimate; not when labelled malicious or someone else operated it", async () => {
    const { applyPatch, emptyPatch, createGraph } = await import("../../../plugins/rootcause/v3/graph.js");
    const g = createGraph("t");
    const add = (p: any, c = cfg) => applyPatch(g, { ...emptyPatch(), ...p }, 1, c);
    add({ addNodes: [
      node("pr", { label: "dev-a created pull request 7 from 198.51.100.67", domain: "github", artifacts: [{ kind: "user", value: "dev-a" }] }),
      node("merge", { label: "dev-a merged pull request 7", domain: "github", legitimate: true, artifacts: [{ kind: "user", value: "dev-a" }] }),
      node("evil", { label: "attacker pushed a stolen token commit as dev-a", domain: "github", artifacts: [{ kind: "user", value: "dev-a" }] }),
    ] });
    for (const id of ["N1", "N2", "N3"]) add(buildAttributionPatch(id, [{ layer: "machine-facing", relation: "ran-as", kind: "account", value: "dev-a", resolvesTo: "github:dev-a", toolCallIds: ["r1"] }], g, { turn: 1, domain: "github", traceIds: () => [] }));
    add({ addEdges: [
      { tempId: "a", kind: "explains", source: "N1", target: "N2", prior: 0.6, evidence: [ev("direct")] },
      { tempId: "b", kind: "explains", source: "N3", target: "N2", prior: 0.6, evidence: [ev("direct")] },
    ] });
    expect(g.nodes.N1!.legitimate).toBe(true);
    expect(g.nodes.N3!.legitimate).toBeFalsy();
  });
});
