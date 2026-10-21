// The projection head carries the report's disposition word once the run has exited, and null while it runs.
import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../../contracts/events.js";
import { emptyPatch, type GraphPatch } from "../../../plugins/rootcause/v3/graph.js";
import { fold, type V3Kinds } from "../../../plugins/rootcause/v3/ledger.js";
import { rootCauseV3Projection } from "../../../plugins/rootcause/v3/projection.js";
import { renderReport } from "../../../plugins/rootcause/v3/report.js";
import { MemoryTrace } from "../../../plugins/rootcause/v3/trace.js";
import type { Alert, ExitReason } from "../../../plugins/rootcause/v3/types.js";
import { apply, cfg, ev, node, seeded } from "./helpers.js";

const alert: Alert = { id: "a1", source: "edr", title: "symptom", observedAt: "2026-05-05T10:00:00Z", artifacts: [], raw: {} };
const link = (source: string): GraphPatch["addEdges"][number] => ({ tempId: "e", kind: "explains", source, target: "N1", prior: 0.4, evidence: [ev("corroborating", "supports", ["elink"])] });
const confirmedUp = (): Partial<GraphPatch> => ({ addNodes: [node("cause", { prior: 0.5, evidence: [ev("direct", "supports", ["c1"])] })], addEdges: [link("cause")] });
const likelyUp = (): Partial<GraphPatch> => ({ addNodes: [node("cause", { prior: 0.5, evidence: [ev("corroborating", "supports", ["c1"])] })], addEdges: [link("cause")] });
const ruledOut = (): Partial<GraphPatch> => ({
  addEvidence: ["f1", "f2", "f3"].map((id) => ({ targetId: "N1", evidence: ev("direct", "refutes", [id]) })),
});

function journal(patches: Partial<GraphPatch>[], exit: ExitReason | null): AgentEvent<V3Kinds>[] {
  const events: AgentEvent<V3Kinds>[] = [];
  let seq = 0;
  const push = (kind: string, payload: unknown) =>
    events.push({ run_id: "r1", run_kind: "root_cause", seq: seq++, ts: "2026-05-05T12:00:00Z", kind, payload: JSON.parse(JSON.stringify(payload)), schema_version: 1 } as AgentEvent<V3Kinds>);
  push("run", { run_kind: "root_cause", spec: { sections: { engine: "rootcause-v3" } } });
  push("rootcause_setup", { cfg, alert, index: "lab", range: ["2026-05-05T00:00:00Z", "2026-05-05T18:00:00Z"], survey: { text: "", domains: [], sizes: {} }, environment: null });
  // seeded() gives the symptom one direct record; the setup's symptom has none until this turn.
  const turns = [{ addEvidence: [{ targetId: "N1", evidence: ev("direct", "supports", ["rs"]) }] }, ...patches];
  turns.forEach((patch, i) => push("rootcause_turn", {
    turn: i + 1,
    entries: [{ id: `T${i}`, ts: `2026-05-05T12:00:0${i}Z`, runId: "r1", turn: i + 1, correlationId: "N1", relatedIds: [], type: "graph-patch", payload: { patch: { ...emptyPatch(), ...patch } } }],
  }));
  if (exit !== null) push("rootcause_exit", { exit, report: "stored", entries: [] });
  return events;
}

// The helper graph and the folded ledger are the same case: one direct record on the symptom, then the patches.
function read(patches: Partial<GraphPatch>[], exit: ExitReason | null) {
  const g = seeded();
  for (const p of patches) apply(g, p);
  const events = journal(patches, exit);
  const folded = fold(events);
  const view = rootCauseV3Projection("r1", events as never);
  const md = folded.graph === null ? "" : renderReport(folded.graph, new MemoryTrace("r1"), exit, alert);
  return { g, view, md };
}

function expectVerdict(patches: Partial<GraphPatch>[], exit: ExitReason, word: string, sentence: string) {
  const { g, view, md } = read(patches, exit);
  expect(view.disposition).toBe(word);
  expect(view.account?.disposition).toBe(sentence);
  expect(md).toContain(`**Disposition:** ${sentence}`);
  return g;
}

describe("root cause v3 disposition on the projection head", () => {
  it("says malicious when the exit is root-cause-found", () => {
    expectVerdict([], "root-cause-found", "malicious", "True Positive: a confirmed causal chain leads from the alert to its root cause.");
  });

  it("says false_positive when the symptom is ruled out and the exit is false-positive", () => {
    const g = expectVerdict([ruledOut()], "false-positive", "false_positive", "False Positive: the alerted event itself was refuted by the evidence.");
    expect(g.nodes.N1!.status).toBe("ruled-out");
  });

  it("follows the sentence when the exit is budget-exhausted: confirmed upstream is malicious, likely is suspicious, neither is inconclusive", () => {
    const confirmed = expectVerdict([confirmedUp()], "budget-exhausted", "malicious", "True Positive (root cause not fully confirmed): 1 confirmed upstream event(s) explain the alert.");
    expect(confirmed.nodes.N2).toMatchObject({ status: "confirmed", kind: "action" });
    const likely = expectVerdict([likelyUp()], "budget-exhausted", "suspicious", "True Positive (causes likely, not yet confirmed): the alert is confirmed and 1 likely upstream event(s) explain it.");
    expect(likely.nodes.N1!.status).toBe("confirmed");
    expect(likely.nodes.N2!.status).not.toBe("confirmed");
    expect(likely.nodes.N2!.confidence).toBeGreaterThanOrEqual(0.6);
    expectVerdict([], "budget-exhausted", "inconclusive", "Inconclusive: no upstream cause of the alert was confirmed.");
  });

  it("says malicious for an inconclusive exit that still has a confirmed upstream event", () => {
    expectVerdict([confirmedUp()], "inconclusive", "malicious", "True Positive (root cause not fully confirmed): 1 confirmed upstream event(s) explain the alert.");
  });

  it("is null while the run has not exited, even with a confirmed upstream event", () => {
    const { view } = read([confirmedUp()], null);
    expect(view.disposition).toBeNull();
    expect(view.exit).toBeNull();
    expect(view.account?.disposition).toMatch(/^True Positive \(root cause not fully confirmed\)/);
  });

  it("does not throw when the exit arrived with no graph", () => {
    const bare = (exit: ExitReason) => {
      const events = [
        { run_id: "r1", run_kind: "root_cause", seq: 0, ts: "2026-05-05T12:00:00Z", kind: "run", payload: {}, schema_version: 1 },
        { run_id: "r1", run_kind: "root_cause", seq: 1, ts: "2026-05-05T12:00:01Z", kind: "rootcause_exit", payload: { exit, report: "", entries: [] }, schema_version: 1 },
      ] as AgentEvent<V3Kinds>[];
      return rootCauseV3Projection("r1", events as never);
    };
    expect(bare("root-cause-found")).toMatchObject({ disposition: "malicious", account: null, exit: "root-cause-found" });
    expect(bare("false-positive")).toMatchObject({ disposition: "false_positive", account: null });
    expect(bare("inconclusive")).toMatchObject({ disposition: "inconclusive", account: null });
    expect(bare("budget-exhausted")).toMatchObject({ disposition: "inconclusive", account: null });
  });
});
