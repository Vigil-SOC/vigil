// A run graph that exercises every report filter: a legitimate user, a victim account used by an unnamed operator,
// an indicator a legitimate step also carries, an unlinked finding's address, a test result that names the
// legitimate user and one worded as an accusation, two story rounds and the model's reasoning.
import { buildAttributionPatch } from "../../../../plugins/rootcause/v3/attribution.js";
import type { Graph } from "../../../../plugins/rootcause/v3/graph.js";
import { MemoryTrace } from "../../../../plugins/rootcause/v3/trace.js";
import type { Alert } from "../../../../plugins/rootcause/v3/types.js";
import { apply, ev, node, seeded } from "../helpers.js";

export const alert: Alert = { id: "a", source: "edr", title: "t", observedAt: "2026-04-02T00:00:00Z", artifacts: [], raw: {} };

export function reportFixture(): { g: Graph; trace: MemoryTrace } {
  const g = seeded();
  const ctx = { turn: 1, domain: "edr", traceIds: () => [] };
  apply(g, { addNodes: [
    node("dev", { label: "devuser applied a config from 192.0.2.7", legitimate: true, prior: 0.5, observedAt: "2026-04-01T10:00:00Z", artifacts: [{ kind: "user", value: "devuser" }, { kind: "ip", value: "192.0.2.7" }], evidence: [ev("direct", "supports", ["d1"]), ev("corroborating", "supports", ["d2"])] }),
    node("use", { label: "calendar entry deleted on host-b7 using j.doe's stolen session", prior: 0.5, observedAt: "2026-04-01T11:00:00Z", artifacts: [{ kind: "ip", value: "192.0.2.7" }, { kind: "ip", value: "198.51.100.9" }, { kind: "host", value: "host-b7" }], evidence: [ev("direct", "supports", ["u1"]), ev("direct", "supports", ["u2"])] }),
    node("lone", { label: "connection from 203.0.113.5 to host-c3", prior: 0.5, observedAt: "2026-04-01T09:00:00Z", artifacts: [{ kind: "ip", value: "203.0.113.5" }, { kind: "user", value: "devuser2" }], evidence: [ev("direct", "supports", ["l1"]), ev("direct", "supports", ["l2"])] }),
  ], addEdges: [
    { tempId: "a", kind: "explains", source: "use", target: "N1", prior: 0.5, evidence: [ev("direct", "supports", ["x1"]), ev("direct", "supports", ["x2"])] },
    { tempId: "b", kind: "explains", source: "dev", target: "use", prior: 0.5, evidence: [ev("direct", "supports", ["x3"]), ev("direct", "supports", ["x4"])] },
  ] });
  apply(g, buildAttributionPatch("N2", [{ layer: "machine-facing", relation: "ran-as", kind: "account", value: "devuser", toolCallIds: ["r1"] }], g, ctx));
  apply(g, buildAttributionPatch("N3", [
    { layer: "machine-facing", relation: "ran-as", kind: "account", value: "j.doe", toolCallIds: ["r1"] },
    { layer: "machine-facing", relation: "operated-by", kind: "operator", value: "intruder via the stolen session", toolCallIds: ["r2"] },
  ], g, ctx));
  const pred = (id: string, status: "found" | "absent", result: string) => ({ id, sourcetype: "edr", expect: "e", spl: "index=x", separates: [], status, result, toolCallIds: ["r9"] });
  g.theories = [
    { id: "R1.H1", round: 1, story: "an outsider took j.doe's session", accountsFor: ["N3"], gaps: "entry point", status: "replaced", support: 1, against: 0, predictions: [pred("R1.P1", "found", "devuser pushed the config at 10:00 from 192.0.2.7")] },
    { id: "R2.H1", round: 2, story: "devuser is the attacker", accountsFor: ["N2", "N3"], gaps: "none", status: "live", support: 0, against: 1, predictions: [pred("R2.P1", "absent", "the attacker left no file on host-b7"), pred("R2.P2", "found", "j.doe's session was used from 198.51.100.9 only")] },
  ] as any;
  const trace = new MemoryTrace("t", () => new Date("2026-04-02T00:00:00Z"));
  trace.forTurn(3, "expand", "N3").write("reasoning", { text: "the session is j.doe's but the address is new; devuser looks involved" });
  trace.forTurn(4, "test", "R2.P1").write("reasoning", { text: "no file found" });
  trace.forTurn(5, "expand", "N2").write("submission", { tool: "submit_causes", submission: { reasoning: "devuser's config push came right before the session use", candidates: [] } });
  return { g, trace };
}
