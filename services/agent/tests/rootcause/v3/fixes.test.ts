// Behaviour added after the first real runs.
import { describe, expect, it } from "vitest";
import { buildAttributionPatch } from "../../../plugins/rootcause/v3/attribution.js";
import { options } from "../../../plugins/rootcause/v3/lead.js";
import { check } from "../../../plugins/rootcause/v3/model/client.js";
import { status } from "../../../plugins/rootcause/v3/scoring.js";
import type { Evidence } from "../../../plugins/rootcause/v3/types.js";
import { createGraph } from "../../../plugins/rootcause/v3/graph.js";
import { apply, cfg, ev, node, seeded } from "./helpers.js";

const e = (strength: Evidence["strength"], ids: string[]): Evidence => ({ id: "V", source: "x", summary: "", strength, polarity: "supports", toolCallIds: ids, traceIds: [], turn: 1 });
const ctx = { turn: 1, domain: "edr", traceIds: () => [] };

describe("confirmation by independent corroboration", () => {
  it("two corroborating items on different searches confirm; on the same search they do not", () => {
    expect(status(0.9, [e("corroborating", ["r1"]), e("corroborating", ["r2"])], cfg)).toBe("confirmed");
    expect(status(0.9, [e("corroborating", ["r1"]), e("corroborating", ["r1", "r9"])], cfg)).toBe("suspected");
  });
});

describe("attribution relations and repeats", () => {
  it("the same identity fact reported again counts once", () => {
    const g = seeded();
    apply(g, { addNodes: [node("act")] });
    const sig = (call: string) => [{ layer: "machine-facing" as const, kind: "account", value: "alice", resolvesTo: "ad:alice", toolCallIds: [call] }];
    apply(g, buildAttributionPatch("N2", sig("r1"), g, ctx));
    const once = Object.values(g.edges)[0]!.confidence;
    apply(g, buildAttributionPatch("N2", sig("r2"), g, ctx), 2);
    expect(Object.values(g.edges)).toHaveLength(1);
    expect(Object.values(g.edges)[0]!.confidence).toBeCloseTo(once, 9);
  });
  it("ran-as the victim and operated-by the attacker are separate edges on one action", () => {
    const g = seeded();
    apply(g, { addNodes: [node("act")] });
    apply(g, buildAttributionPatch("N2", [
      { layer: "machine-facing", relation: "ran-as", kind: "account", value: "alice", resolvesTo: "ad:alice", toolCallIds: ["r1"] },
      { layer: "machine-facing", relation: "operated-by", kind: "operator", value: "evil.test operator", toolCallIds: ["r2"] },
    ], g, ctx));
    const kinds = Object.values(g.edges).map((x) => `${x.kind}:${g.nodes[x.source]!.label}:${x.status}`).sort();
    expect(kinds).toEqual(["attributed-to:ad:alice:confirmed", "operated-by:evil.test operator:suspected"]);
    expect(Object.values(g.edges).every((x) => !x.disputed)).toBe(true);
  });
  it("an actor reported under a new name for the same principal is reused", () => {
    const g = seeded();
    apply(g, { addNodes: [node("a1"), node("a2")] });
    apply(g, buildAttributionPatch("N2", [{ layer: "machine-facing", kind: "account", value: "user-1", toolCallIds: ["r1"] }], g, ctx));
    apply(g, buildAttributionPatch("N3", [{ layer: "machine-facing", kind: "account", value: "user-1", resolvesTo: "ad:CORP\\user-1", toolCallIds: ["r1"] }], g, ctx));
    expect(Object.values(g.nodes).filter((n) => n.kind === "actor")).toHaveLength(1);
  });
});

describe("joint causes", () => {
  it("do not dispute each other", () => {
    const g = seeded();
    apply(g, { addNodes: [node("mail"), node("click")], addEdges: [
      { tempId: "a", kind: "explains", source: "mail", target: "N1", prior: 0.3, evidence: [] },
      { tempId: "b", kind: "explains", source: "click", target: "N1", prior: 0.3, evidence: [], joint: true }] });
    expect(Object.values(g.edges).some((x) => x.disputed)).toBe(false);
  });
});

describe("branch availability", () => {
  it("is offered from every live event except the symptom, until an event has been tried twice", () => {
    const g = seeded();
    apply(g, { addNodes: [node("mid"), node("root", { isRoot: true })], addEdges: [{ tempId: "e1", kind: "explains", source: "mid", target: "N1", prior: 0.3, evidence: [] }, { tempId: "e2", kind: "explains", source: "root", target: "mid", prior: 0.3, evidence: [] }] });
    expect(options(g, cfg).branch).toEqual(["N2", "N3"]);
    apply(g, { incrementBranch: ["N3"] }, 2);
    apply(g, { incrementBranch: ["N3"] }, 3);
    expect(options(g, cfg).branch).toEqual(["N2"]);
  });
});

describe("citations", () => {
  it("refuses tool call ids never issued in the run", () => {
    const sub = { evidence: [{ summary: "x", strength: "direct", polarity: "supports", toolCallIds: ["r9"] }], reasoning: "r" };
    expect(check("submit_assessment", sub, 3)).toMatchObject({ success: false });
    expect(check("submit_assessment", sub, 9)).toMatchObject({ success: true });
  });
});

describe("duplicates", () => {
  const at = (t: string) => `2026-04-02T${t}Z`;
  it("the same connection re-described a few seconds apart merges", () => {
    const g = seeded();
    const ips = [{ kind: "ip" as const, value: "192.0.2.14" }, { kind: "ip" as const, value: "203.0.113.66" }];
    apply(g, { addNodes: [node("x1", { label: "4.8GB out to 203.0.113.66", domain: "zeek:conn", observedAt: at("13:11:00"), artifacts: ips })] });
    const r = apply(g, { addNodes: [node("x2", { label: "exfiltration of the staged archive", domain: "zeek:conn", observedAt: at("13:11:05"), artifacts: ips })] });
    expect(r.idMap.x2).toBe("N2");
  });
  it("the same command line on the same host merges, even with a time missing", () => {
    const g = seeded();
    const arts = [{ kind: "host" as const, value: "WKS-014" }, { kind: "process" as const, value: "7z.exe a -p C:\\Users\\Public\\data.7z \\\\SRV-01\\Share$\\*" }];
    apply(g, { addNodes: [node("a", { label: "7z archived a share", observedAt: at("13:08:00"), artifacts: arts })] });
    expect(apply(g, { addNodes: [node("b", { label: "staging data for exfil", artifacts: arts })] }).idMap.b).toBe("N2");
  });
  it("different commands on one host, or a shared parent process id, do not merge", () => {
    const g = seeded();
    const host = { kind: "host" as const, value: "WKS-014" }, parent = { kind: "process-guid" as const, value: "4242" };
    apply(g, { addNodes: [node("v", { label: "vssadmin", observedAt: at("14:02:11"), artifacts: [host, parent, { kind: "process", value: "vssadmin.exe delete shadows /all /quiet" }] })] });
    const r = apply(g, { addNodes: [node("w", { label: "wbadmin", observedAt: at("14:02:40"), artifacts: [host, parent, { kind: "process", value: "wbadmin.exe delete catalog -quiet" }] })] });
    expect(r.idMap.w).toBe("N3");
  });
});

describe("end of run", () => {
  const solid = (ids: string[]) => [ev("direct", "supports", [ids[0]!]), ev("corroborating", "supports", [ids[1]!])];
  it("a chain link is settled once two checks do not move it, or once it is near-certain", () => {
    const g = seeded();
    apply(g, { addNodes: [node("root", { isRoot: true, evidence: solid(["r1", "r2"]) })], addEdges: [{ tempId: "e", kind: "explains", source: "root", target: "N1", prior: 0.5, evidence: [ev("corroborating", "supports", ["k1"])] }] });
    expect(options(g, cfg).strengthen).toContain("E1");
    apply(g, { incrementAttempts: ["E1"] }, 2); // one check found nothing new: it gets another
    expect(options(g, cfg).strengthen).toContain("E1");
    apply(g, { incrementAttempts: ["E1"] }, 3); // two unmoved checks: settled
    expect(options(g, cfg).strengthen).not.toContain("E1");
    const g2 = seeded();
    apply(g2, { addNodes: [node("root", { isRoot: true, evidence: solid(["r1", "r2"]) })], addEdges: [{ tempId: "e", kind: "explains", source: "root", target: "N1", prior: 0.5, evidence: [ev("corroborating", "supports", ["k1"]), ...["a", "b", "c", "d", "e", "f"].map((x) => ev("circumstantial", "supports", [x], `timing ${x}`))] }] });
    expect(g2.edges.E1!.status).toBe("suspected");
    expect(g2.edges.E1!.confidence).toBeGreaterThanOrEqual(0.95);
    expect(options(g2, cfg).strengthen).not.toContain("E1");
  });
});

describe("victim accounts and dead ends", () => {
  it("dead-end events get no branch move", () => {
    const g = seeded();
    apply(g, { setFlags: [{ nodeId: "N1", isRoot: true }], incrementBranch: ["N1"] });
    apply(g, { addNodes: [node("wipe", { deadEnd: true, prior: 0.5, evidence: [ev("direct", "supports", ["w1"])] })] });
    expect(options(g, cfg).branch).toBeUndefined();
  });
  it("the report names a victim account apart from the attacker who used it", async () => {
    const { renderReport } = await import("../../../plugins/rootcause/v3/report.js");
    const { MemoryTrace } = await import("../../../plugins/rootcause/v3/trace.js");
    const g = seeded();
    apply(g, { addNodes: [node("repl", { label: "directory replication using alice's stolen credentials", evidence: [ev("direct", "supports", ["d1"])] })] });
    apply(g, buildAttributionPatch("N2", [
      { layer: "machine-facing", relation: "ran-as", kind: "account", value: "alice", toolCallIds: ["r1"] },
      { layer: "machine-facing", relation: "operated-by", kind: "operator", value: "evil.test operator", toolCallIds: ["r2"] },
    ], g, ctx));
    const md = renderReport(g, new MemoryTrace("t"), null, { id: "a", source: "edr", title: "t", observedAt: "2026-04-02T00:00:00Z", artifacts: [], raw: {} });
    expect(md).toMatch(/- the attacker \(an operator the logs do not name\): attacker \(operated the malicious actions\); seen as evil\.test/);
    expect(md).toMatch(/- alice: victim account, used by the attacker/);
    expect(md).toMatch(/ran as alice, operated by evil\.test operator/);
  });
});

describe("cross-source sweep", () => {
  it("sweeps artifacts, ARN role/session names and the accounts the event ran as", async () => {
    const { sweepValues } = await import("../../../plugins/rootcause/v3/sweep.js");
    const g = seeded();
    apply(g, { addNodes: [node("x", { artifacts: [{ kind: "arn", value: "arn:aws:sts::1:assumed-role/deployer/ci-session" }, { kind: "ip", value: "192.0.2.9" }, { kind: "process-guid", value: "77" }] })] });
    apply(g, buildAttributionPatch("N2", [{ layer: "machine-facing", kind: "account", value: "svc-a", toolCallIds: ["r1"] }], g, ctx));
    expect(sweepValues(g, g.nodes.N2!)).toEqual(["deployer", "ci-session", "arn:aws:sts::1:assumed-role/deployer/ci-session", "192.0.2.9", "svc-a"]);
  });
  it("finds the source that pins the event and opens a boundary there when a cause search comes back empty", async () => {
    const { sweep } = await import("../../../plugins/rootcause/v3/sweep.js");
    const g = seeded();
    apply(g, { addNodes: [node("x", { domain: "edr", artifacts: [{ kind: "file-hash", value: "ABCD1234" }, { kind: "user", value: "bob" }] })] });
    const calls: string[] = [];
    const tools = { callCount: 0, list: () => [], call: async (_: string, input: any) => {
      calls.push(input.spl);
      const data = input.spl.includes("ABCD1234") ? '4 events matched\n{"sourcetype":"edr","count":"3","first":"1","last":"1"}\n{"sourcetype":"mail","count":"1","first":"1","last":"1"}' : '90 events matched\n{"sourcetype":"auth","count":"90","first":"1","last":"2"}';
      return { toolCallId: `r${calls.length}`, tool: "splunk_query", input, result: { ok: true, data } };
    } };
    const { MemoryTrace } = await import("../../../plugins/rootcause/v3/trace.js");
    const r = await sweep(g, g.nodes.N2!, { cfg, tools, trace: new MemoryTrace("t").forTurn(1, "expand", "N2"), domains: ["edr", "mail", "auth"], range: ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"], index: "lab" } as any);
    // One presence lookup per value, shared with the coverage check: the event's own source is left out here, in code.
    expect(calls[0]).toMatch(/^index=lab sourcetype=\* \("ABCD1234" OR host="ABCD1234"\) \| stats count .* by sourcetype$/);
    expect(r.topDomain).toBe("mail");
    expect(r.leads).toMatch(/"ABCD1234": mail 1 events .*\[r1\]/);
    expect(r.leads).not.toMatch(/edr/);
  });
});

describe("same event, times, placeholders", () => {
  const arts = [{ kind: "arn" as const, value: "arn:aws:sts::1:assumed-role/ctl/deploy" }, { kind: "k8s-resource" as const, value: "fn-x" }];
  it("the same API call described twice (same source, within 5 s, two shared identifiers) is one event", async () => {
    const { sameEvent } = await import("../../../plugins/rootcause/v3/stitch.js");
    expect(sameEvent({ domain: "ct", observedAt: "2026-01-01T10:00:00Z", artifacts: arts }, { domain: "ct", observedAt: "2026-01-01T10:00:03Z", artifacts: arts })).toBe(true);
    expect(sameEvent({ domain: "ct", observedAt: "2026-01-01T10:00:00Z", artifacts: arts }, { domain: "ct", observedAt: "2026-01-01T10:00:09Z", artifacts: arts })).toBe(false);
    expect(sameEvent({ domain: "ct", observedAt: "2026-01-01T10:00:00Z", artifacts: [arts[0]!, { kind: "host", value: "h" }] }, { domain: "ct", observedAt: "2026-01-01T10:00:00Z", artifacts: [arts[0]!, { kind: "host", value: "h" }] })).toBe(false);
    const g = seeded();
    apply(g, { addNodes: [node("a", { domain: "ct", observedAt: "2026-01-01T10:00:00Z", artifacts: arts })] });
    expect(apply(g, { addNodes: [node("b", { label: "other words", domain: "ct", observedAt: "2026-01-01T10:00:01Z", artifacts: arts })] }).idMap.b).toBe("N2");
  });
  it("an event time that is not in the cited results is dropped", async () => {
    const { Vetter } = await import("../../../plugins/rootcause/v3/handlers/common.js");
    const { MemoryTrace } = await import("../../../plugins/rootcause/v3/trace.js");
    const ctx: any = { turn: 1, tools: { callCount: 1 }, trace: new MemoryTrace("t").forTurn(1, "branch", "N1") };
    const v = new Vetter(ctx, [{ id: "r1", tool: "splunk_query", input: {}, ok: true, traceId: "T1", text: '{"_time":"2026-04-02T08:31:05.000+00:00"} {"first":"1775118673"}' }]);
    expect(v.checkTime("2026-04-02T08:31:05Z", ["r1"], "x")).toBe("2026-04-02T08:31:05Z");
    expect(v.checkTime("2026-04-02T08:32:00Z", ["r1"], "x")).toBe("2026-04-02T08:32:00Z"); // epoch 1775118673 = 08:31:13Z, 47 s away
    expect(v.checkTime("2026-04-02T13:11:00Z", ["r1"], "x")).toBeUndefined();
  });
  it("a boundary's placeholder link is never strengthened", () => {
    const g = seeded();
    apply(g, { addNodes: [node("m", { kind: "boundary-marker", originNodeId: "N1", targetDomain: "mail" })], addEdges: [{ tempId: "e", kind: "explains", source: "m", target: "N1", prior: 0.3, evidence: [] }] });
    apply(g, { updateMarkers: [{ markerId: "B1", pivotSteps: cfg.maxForwardSteps }] });
    expect(options(g, cfg).strengthen).not.toContain("E1");
  });
});

describe("over-long submissions", () => {
  it("are trimmed, not rejected", () => {
    const sub = { evidence: [{ summary: "x".repeat(500), strength: "direct", polarity: "supports", toolCallIds: ["r1"] }], reasoning: "y".repeat(2000) };
    const r = check("submit_assessment", sub, 1);
    expect(r.success).toBe(true);
    expect(((r as any).data.evidence[0].summary as string).length).toBe(300);
  });
});

describe("identity chains", () => {
  it("a role, its assumed session, a same-named function and the carrying service do not dispute", () => {
    const g = seeded();
    apply(g, { addNodes: [node("act")] });
    const sig = (value: string, call: string) => ({ layer: "machine-facing" as const, kind: "iam-principal", value, toolCallIds: [call] });
    apply(g, buildAttributionPatch("N2", [sig("arn:aws:sts::1111:assumed-role/admin/fn-sync", "r1")], g, ctx));
    apply(g, buildAttributionPatch("N2", [sig("arn:aws:iam::1111:role/admin", "r2")], g, ctx), 2);
    apply(g, buildAttributionPatch("N2", [sig("arn:aws:lambda:us-east-1:1111:function:fn-sync", "r3")], g, ctx), 3);
    apply(g, buildAttributionPatch("N2", [sig("lambda.amazonaws.com", "r4")], g, ctx), 4);
    expect(Object.values(g.edges).filter((e) => e.target === "N2").some((e) => e.disputed)).toBe(false);
    apply(g, buildAttributionPatch("N2", [sig("arn:aws:iam::1111:user/mallory", "r5")], g, ctx), 5);
    expect(Object.values(g.edges).filter((e) => e.target === "N2" && e.disputed).length).toBeGreaterThan(0);
  });
});

describe("accusing labels need a baseline", () => {
  const cand = (label: string, baseline?: object) => ({ label, domain: "eks", prior: 0.4, artifacts: [], nodeEvidence: [], edgeEvidence: [], isRootCause: false, ...(baseline ? { baseline } : {}) });
  it("refuses 'attacker ...' without a baseline, accepts it with one, and accepts neutral wording", () => {
    expect(check("submit_causes", { candidates: [cand("Attacker used stolen credential from 1.2.3.4")], reasoning: "r" }, 3)).toMatchObject({ success: false });
    // A takeover claim ("stolen credential") also needs the takeover record (v3-temp attribution rule).
    expect(check("submit_causes", { candidates: [cand("Attacker used stolen credential from 1.2.3.4", { summary: "s", unusual: true, toolCallIds: ["r1"] })], reasoning: "r" }, 3)).toMatchObject({ success: false });
    expect(check("submit_causes", { candidates: [{ ...cand("Attacker used stolen credential from 1.2.3.4", { summary: "s", unusual: true, toolCallIds: ["r1"] }), takeover: { summary: "key created for the account from 1.2.3.4", toolCallIds: ["r2"] } }], reasoning: "r" }, 3)).toMatchObject({ success: true });
    expect(check("submit_causes", { candidates: [cand("Attacker ran kubectl from 1.2.3.4", { summary: "s", unusual: true, toolCallIds: ["r1"] })], reasoning: "r" }, 3)).toMatchObject({ success: true });
    expect(check("submit_causes", { candidates: [cand("kubectl create from 1.2.3.4 as role/admin")], reasoning: "r" }, 3)).toMatchObject({ success: true });
  });
});

describe("legitimate steps", () => {
  it("are never the root, keep the chain open upstream, and their account is reported as not the attacker", async () => {
    const g = seeded();
    apply(g, { addNodes: [node("dev", { label: "admin applied a Function resource from 6.6.6.6", legitimate: true, isRoot: true, evidence: [ev("direct", "supports", ["d1"]), ev("corroborating", "supports", ["d2"])] })],
      addEdges: [{ tempId: "e", kind: "explains", source: "dev", target: "N1", prior: 0.5, evidence: [ev("direct", "supports", ["x1"]), ev("corroborating", "supports", ["x2"])] }] });
    expect(g.nodes.N2!.isRoot).toBeUndefined();
    expect(options(g, cfg).expand).toContain("N2");
    apply(g, buildAttributionPatch("N2", [{ layer: "machine-facing", kind: "account", value: "k8s:admin", toolCallIds: ["r1"] }], g, ctx));
    const { renderReport } = await import("../../../plugins/rootcause/v3/report.js");
    const { MemoryTrace } = await import("../../../plugins/rootcause/v3/trace.js");
    const md = renderReport(g, new MemoryTrace("t"), null, { id: "a", source: "edr", title: "t", observedAt: "2026-01-01T00:00:00Z", artifacts: [], raw: {} });
    expect(md).toMatch(/k8s:admin: legitimate user/);
    expect(md).toMatch(/legitimate activity, not the attacker/);
  });
});

describe("linked system identities", () => {
  it("a role and its session reported together both start high and confirm on one record", () => {
    const g = seeded();
    apply(g, { addNodes: [node("act")] });
    apply(g, buildAttributionPatch("N2", [
      { layer: "machine-facing", kind: "iam-principal", value: "arn:aws:iam::1111:role/admin", toolCallIds: ["r1"] },
      { layer: "machine-facing", kind: "iam-principal", value: "arn:aws:sts::1111:assumed-role/admin/fn-sync", toolCallIds: ["r1"] },
    ], g, ctx));
    expect(Object.values(g.edges).every((e) => e.status === "confirmed")).toBe(true);
  });
});

describe("search coverage", () => {
  it("counts the sourcetypes a search reads, not the ones it excludes", async () => {
    const { searchedSourcetypes } = await import("../../../plugins/rootcause/v3/tools/registry.js");
    expect(searchedSourcetypes({ spl: `index=x sourcetype="aws:cloudtrail" foo | stats count` })).toEqual(["aws:cloudtrail"]);
    expect(searchedSourcetypes({ spl: `index=x NOT sourcetype="aws:cloudtrail" "v" | stats count by sourcetype` })).toEqual([]);
    expect(searchedSourcetypes({ spl: `index=x (sourcetype=a OR sourcetype=b)` })).toEqual(["a", "b"]);
    expect(searchedSourcetypes({ spl: `index=x sourcetype IN ("a", b) NOT sourcetype IN (c)` })).toEqual(["a", "b"]);
    expect(searchedSourcetypes({ spl: `index=x sourcetype=* ("v" OR host="v") | stats count by sourcetype` })).toEqual([]);
  });
  it("names small sources in the constraints", async () => {
    const { coverageLine } = await import("../../../plugins/rootcause/v3/model/prompts.js");
    expect(coverageLine(["big", "gh"], { big: 40 }, { big: 2_000_000, gh: 393 })).toMatch(/big 40; gh 0 \(small, 393 events/);
  });
});

describe("pivot closes when a lead already explains the origin", () => {
  it("resolves the boundary without another model turn", async () => {
    const { pivot } = await import("../../../plugins/rootcause/v3/handlers/pivot.js");
    const { MemoryTrace } = await import("../../../plugins/rootcause/v3/trace.js");
    const g = seeded();
    const strong = [ev("direct", "supports", ["s1"]), ev("corroborating", "supports", ["s2"])];
    apply(g, { addNodes: [node("m", { kind: "boundary-marker", originNodeId: "N1", targetDomain: "k8s" }), node("lead", { domain: "k8s" })],
      addEdges: [{ tempId: "e", kind: "explains", source: "m", target: "N1", prior: 0.3, evidence: [] }, { tempId: "s", kind: "explains", source: "lead", target: "N1", prior: 0.4, evidence: strong }] });
    apply(g, { updateMarkers: [{ markerId: "B1", pivotSteps: 2, forwardLeaves: ["N2"] }] });
    const model = { runTurn: async () => { throw new Error("no model turn expected"); } };
    const ctx: any = { cfg, turn: 3, model, tools: { callCount: 0 }, trace: new MemoryTrace("t").forTurn(3, "pivot", "B1"), domains: ["edr", "k8s"], range: ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"] };
    const patch = await pivot({ move: "pivot", targetId: "B1", turn: 3, reason: "" }, g, ctx);
    apply(g, patch, 3);
    expect(g.nodes.B1!.resolvedTo).toBe("N2");
  });
});

describe("disposition", () => {
  it("a confirmed alert with likely causes is a true positive, not inconclusive", async () => {
    const { disposition } = await import("../../../plugins/rootcause/v3/report.js");
    const g = seeded();
    apply(g, { addNodes: [node("cause", { prior: 0.5, evidence: [ev("corroborating", "supports", ["c1"])] })], addEdges: [{ tempId: "e", kind: "explains", source: "cause", target: "N1", prior: 0.5, evidence: [ev("corroborating", "supports", ["c2"])] }] });
    g.nodes.N1!.status = "confirmed";
    expect(g.nodes.N2!.status).toBe("suspected");
    expect(disposition(g, "budget-exhausted").sentence).toMatch(/^True Positive \(causes likely/);
  });
});

describe("submissions are not thrown away whole", () => {
  const ev1 = { label: "curl wrote /tmp/job.yaml on node-1", domain: "edr", prior: 0.4, artifacts: [], nodeEvidence: [], edgeEvidence: [] };
  const test = (events: unknown) => ({ outcome: "found", summary: "s", toolCallIds: ["r1"], supports: [], weakens: [], events, reasoning: "r" });
  it("accepts events sent as a JSON string and without isRootCause", () => {
    expect(check("submit_test", test(JSON.stringify([ev1])), 3)).toMatchObject({ success: true, data: { events: [{ label: ev1.label }] } });
  });
  it("'recon job' is a name, not an accusation", () => {
    expect(check("submit_test", test([{ ...ev1, label: "recon job started on node-1" }]), 3)).toMatchObject({ success: true });
  });
  it("salvage drops only the bad item", async () => {
    const { salvage } = await import("../../../plugins/rootcause/v3/model/client.js");
    const input = test([ev1, { ...ev1, label: "credential minted outside captured telemetry" }]);
    const first = check("submit_test", input, 3);
    expect(first).toMatchObject({ success: false, drop: [["events", 1]] });
    expect(salvage("submit_test", input, 3, {}, first)).toMatchObject({ dropped: 1, data: { events: [{ label: ev1.label }] } });
  });
});

describe("baselines are checked, not taken on the model's word", () => {
  const range: [string, string] = ["2026-03-10T00:00:00Z", "2026-03-13T00:00:00Z"];
  const calls: Record<string, unknown> = {
    r1: { spl: "index=i sourcetype=github actor=dev-a | stats count by action", earliest: range[0], latest: range[1] },
    r2: { spl: "index=i sourcetype=github | stats count by actor", earliest: range[0], latest: range[1] }, // other accounts, not his own
    r3: { spl: "index=i sourcetype=github actor=dev-a | stats count", earliest: "2026-03-12T10:00:00Z", latest: range[1] }, // a few hours
  };
  const cx = { inputOf: (id: string) => calls[id], range };
  const cand = (ids: string[], unusual = true) => ({ label: "register_self_hosted_runner by dev-a", domain: "github", prior: 0.4, artifacts: [{ kind: "user", value: "dev-a" }], nodeEvidence: [], edgeEvidence: [], baseline: { summary: "s", unusual, toolCallIds: ids } });
  const sub = (c: object) => ({ effects: [c], reasoning: "r" });
  it("needs a whole-range search naming the account, both for unusual and for routine; a baseline that fails is dropped and the event tagged unverified", () => {
    const kept = (r: ReturnType<typeof check>) => (r as { data: { effects: { baseline?: unknown; unverified?: boolean }[] } }).data.effects[0]!;
    expect(kept(check("submit_effects", sub(cand(["r1"])), 3, cx))).toMatchObject({ baseline: { unusual: true } });
    for (const e of [kept(check("submit_effects", sub(cand(["r2"])), 3, cx)), kept(check("submit_effects", sub(cand(["r3"], false)), 3, cx))]) {
      expect(e.unverified).toBe(true);
      expect(e.baseline).toBeUndefined();
    }
  });
  it("drops an item citing a ref the run never issued, and keeps the rest", async () => {
    const { salvage } = await import("../../../plugins/rootcause/v3/model/client.js");
    const two = sub({ ...cand(["r1"]), label: "second" });
    const input = { effects: [cand(["r1"]), { ...cand(["r9"]), label: "second" }, two.effects[0]], reasoning: "r" };
    const first = check("submit_effects", input, 3, cx);
    expect(first).toMatchObject({ success: false, drop: [["effects", 1]] });
    expect(salvage("submit_effects", input, 3, cx, first)).toMatchObject({ dropped: 1, data: { effects: [{ label: "register_self_hosted_runner by dev-a" }, { label: "second" }] } });
  });
});

describe("a cause is not merged into what it causes", () => {
  const key = { kind: "access-key" as const, value: "ASIAEXAMPLEKEY01" }, role = { kind: "arn" as const, value: "arn:aws:iam::1:role/admin" };
  it("keeps the AssumeRole one second before the DeleteRule as its own cause, and different event ids apart", () => {
    const g = createGraph("t");
    apply(g, { addNodes: [node("del", { isSymptom: true, label: "DeleteRule", domain: "ct", observedAt: "2026-03-13T12:34:40Z", artifacts: [key, role, { kind: "event-id", value: "e-del" }] })] });
    apply(g, { addNodes: [node("ar", { label: "AssumeRole minting the key", domain: "ct", observedAt: "2026-03-13T12:34:39Z", artifacts: [key, role] })],
      addEdges: [{ tempId: "x", kind: "explains", source: "ar", target: "N1", prior: 0.3, evidence: [] }] });
    expect(Object.keys(g.nodes)).toEqual(["N1", "N2"]);
    expect(Object.values(g.edges).map((e) => [e.source, e.target])).toEqual([["N2", "N1"]]);
    apply(g, { addNodes: [node("other", { label: "DeleteRule", domain: "ct", observedAt: "2026-03-13T12:34:40Z", artifacts: [key, { kind: "event-id", value: "e-other" }] })] });
    expect(Object.keys(g.nodes)).toHaveLength(3);
  });
});

describe("the same process re-described merges", () => {
  const host = { kind: "host" as const, value: "node-1" }, guid = { kind: "process-guid" as const, value: "111" };
  it("same guid + host + second + container merges; same guid alone (a parent recorded as own id) does not", () => {
    const g = createGraph("t");
    apply(g, { addNodes: [node("a", { label: "runc create of container c1", observedAt: "2026-03-11T15:54:16Z", artifacts: [guid, host, { kind: "k8s-resource", value: "c1" }] })] });
    apply(g, { addNodes: [node("b", { label: "Container c1 launched", observedAt: "2026-03-11T15:54:16Z", artifacts: [guid, host, { kind: "k8s-resource", value: "c1" }] })] });
    expect(Object.keys(g.nodes)).toEqual(["N1"]);
    apply(g, { addNodes: [node("c", { label: "another child", observedAt: "2026-03-11T15:54:17Z", artifacts: [guid, host] })] });
    expect(Object.keys(g.nodes)).toEqual(["N1", "N2"]);
  });
});

describe("rare outliers get tested, absent needs the whole range, routine must be shown", async () => {
  const { rareValues, mentionsRare } = await import("../../../plugins/rootcause/v3/rare.js");
  const { routineShown } = await import("../../../plugins/rootcause/v3/baseline.js");
  const rare = `## Rare activity\n\n- edr:process:\n  ParentBaseFileName (39 distinct) [r6]: tool-a ×2 (2026-03-11 15:52 → 2026-03-11 15:52, node-1); xz ×5 (2026-03-11 15:54 → 2026-03-11 16:30, node-1); tool-b ×13 (2026-03-11 15:54, node-1)\n- git:audit:\n  actor (3 distinct) [r15]: outsider1 ×3 (2026-03-11 14:53 → 2026-03-11 14:54, sim)`;
  it("parses the rare list (few-times values only) and matches short names as words", () => {
    expect(rareValues(rare).map((x) => [x.value, x.sourcetype])).toEqual([["tool-a", "edr:process"], ["xz", "edr:process"], ["outsider1", "git:audit"]]);
    expect(mentionsRare('index=i ParentBaseFileName="xz"', ["xz"])).toBe(true);
    expect(mentionsRare("index=i tar.xzf work", ["xz"])).toBe(false);
  });
  it("routine needs the baseline's results spread over half the range", () => {
    const range: [string, string] = ["2026-03-11T00:00:00Z", "2026-03-14T00:00:00Z"];
    const c = (ids: string[]) => ({ label: "x", domain: "d", prior: 0.3, artifacts: [], nodeEvidence: [], edgeEvidence: [], baseline: { summary: "s", unusual: false, toolCallIds: ids } });
    const head = "purpose: p window: 2026-03-11T00:00:00.000Z → 2026-03-14T00:00:00.000Z (as requested) 6 events matched\n";
    const out: Record<string, string> = { r1: head + '{"first":"1773159600","last":"1773404800"}', r2: head + '{"first":"2026-03-12 12:46"}\n{"last":"2026-03-13 12:34"}' };
    expect(routineShown(c(["r1"]), (id) => out[id], range)).toBe(true); // Mar 11 03:00 -> Mar 13 12:26
    expect(routineShown(c(["r2"]), (id) => out[id], range)).toBe(false); // one day of three
    expect(routineShown(c(["r3"]), (id) => ({ r3: head })[id], range)).toBe(false); // the header window alone is not activity
  });
});

describe("a test is absent only after a whole-range search", () => {
  it("downgrades a narrow-window absent to unclear and drops its weakens", async () => {
    const { test: testHandler } = await import("../../../plugins/rootcause/v3/handlers/test.js");
    const { MemoryTrace } = await import("../../../plugins/rootcause/v3/trace.js");
    const g = seeded();
    g.theories = [{ id: "R1.H1", round: 1, story: "s", accountsFor: [], gaps: "", status: "live", support: 0, against: 0, predictions: [{ id: "R1.P1", sourcetype: "edr", expect: "e", spl: "index=x sourcetype=edr", separates: [], next: true, status: "open" }] }];
    const inputs: Record<string, unknown> = { r1: { spl: "index=x sourcetype=edr", earliest: "2026-03-13T11:00:00Z", latest: "2026-03-13T12:40:00Z" } };
    const sub = { outcome: "absent", summary: "nothing", toolCallIds: ["r1"], supports: [], weakens: ["R1.H1"], events: [], reasoning: "" };
    const ctx: any = { model: { runTurn: async () => ({ submission: sub, toolCalls: [] }) }, trace: new MemoryTrace("t").forTurn(2, "test", "R1.P1"), cfg, range: ["2026-03-11T00:00:00Z", "2026-03-14T00:00:00Z"], domains: ["edr"], tools: { callCount: 1, coverage: {}, inputOf: (id: string) => inputs[id] }, sizes: {} };
    const patch = await testHandler({ move: "test", targetId: "R1.P1", turn: 2, reason: "" }, g, ctx);
    expect(patch.predictionResult).toMatchObject({ status: "unclear", weakens: [] });
  });
});

describe("one boundary per domain for the same process described twice", () => {
  it("refuses a second marker into the same domain from a copy of the same container start", async () => {
    const { boundaryPatch } = await import("../../../plugins/rootcause/v3/handlers/common.js");
    const { MemoryTrace } = await import("../../../plugins/rootcause/v3/trace.js");
    const g = createGraph("t");
    const arts = [{ kind: "host" as const, value: "node-1" }, { kind: "k8s-resource" as const, value: "c1" }];
    apply(g, { addNodes: [node("a", { label: "runc create c1", artifacts: arts }), node("b", { label: "container c1 launched", observedAt: "2026-03-11T15:59:00Z", artifacts: arts })] });
    const ctx: any = { trace: new MemoryTrace("t").forTurn(1, "expand", "N1"), domains: ["edr", "github"] };
    const b = { targetDomain: "github", reason: "r", inDomainExhausted: true };
    apply(g, boundaryPatch(b, "N1", g, ctx) as any);
    expect(Object.values(g.nodes).filter((n) => n.kind === "boundary-marker")).toHaveLength(1);
    expect(boundaryPatch(b, "N2", g, ctx)).toEqual({});
  });
});
