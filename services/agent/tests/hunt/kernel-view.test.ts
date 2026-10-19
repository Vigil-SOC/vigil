import { describe, expect, it } from "vitest";
import type { CallPayload } from "../../contracts/events.js";
import { InProcessState } from "../../core/state.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import type { HuntKinds } from "../../plugins/hunt/ledger.js";
import { runHunt } from "../../plugins/hunt/workflow.js";
import { huntKernelReplay, huntView } from "../../plugins/hunt/view.js";
import { turnWords } from "../../plugins/hunt/words.js";
import { RUN, specOf, modelOf, harnessOf, type Step } from "../support/kernel-hunt.js";

const call = (tool: string, args: unknown, extra: Partial<CallPayload> = {}): CallPayload => ({
  ref: `r-${tool}`,
  turn: 1,
  call_id: "c1",
  tool,
  arguments: JSON.stringify(args),
  result: "",
  ok: true,
  failure: null,
  ...extra,
});

describe("a kernel hunt's turn, in words", () => {
  it("says what a search asked, never its JSON", () => {
    expect(turnWords([call("search_findings", { query: "HTTPS 60s interval", limit: 20 })])).toEqual({ action: "QUERY", words: "Searched alerts for HTTPS 60s interval" });
    const recall = turnWords([call("recall_entity", { entity_keys: ["ip:203.0.113.47", "ip:198.51.100.23"], caller_kind: "hunt_lead", caller_id: "threat-hunt-h-1" })]);
    expect(recall.words).toBe("Recalled what memory holds on ip:203.0.113.47 and ip:198.51.100.23");
    expect(recall.words).not.toContain("caller");
  });

  it("folds a turn's searches into one line, naming the rest by count", () => {
    const turn = ["203.0.113.47", "cdn-invoicesync.net", "msupdate64.dll", "rundll32", "Invoice_Q3"].map((query) => call("search_findings", { query }));
    expect(turnWords(turn).words).toBe("Searched alerts for 203.0.113.47, cdn-invoicesync.net, msupdate64.dll and 2 more");
  });

  it("is named for the turn's most consequential move", () => {
    const turn = [call("search_findings", { query: "svc_backup" }), call("record_evidence", { results: [{ summary: "SMB to FS-PAY-02 as svc_backup" }] })];
    expect(turnWords(turn)).toEqual({ action: "RECORD", words: "Recorded 1 piece of evidence: SMB to FS-PAY-02 as svc_backup (and 1 other call)" });
  });

  it("gives each move kind its own label", () => {
    const labels = [
      ["record_evidence", { results: [] }],
      ["link_evidence", { links: [] }],
      ["propose_hypothesis", { statement: "x" }],
      ["validate_hypothesis", { hypothesis_id: "h-1" }],
      ["expand", { evidence_ids: ["ev-1"] }],
      ["lookup_indicators", { values: ["1.2.3.4"] }],
    ].map(([tool, args]) => turnWords([call(tool as string, args)]).action);
    expect(labels).toEqual(["RECORD", "LINK", "PROPOSE", "VALIDATE", "EXPAND", "QUERY"]);
  });

  it("names an unknown tool by its words and the value it was asked about", () => {
    expect(turnWords([call("dns_history", { value: "update-ledgerbox.com" })]).words).toBe("Ran dns history on update-ledgerbox.com");
    expect(turnWords([call("conclude_hunt", { reason: "done" }, { ok: false })]).words).toBe("Asked to conclude the hunt (refused)");
  });
});

describe("a kernel hunt's moves", () => {
  async function hunted(steps: Step[]) {
    const state = new InProcessState<HuntKinds>();
    await runHunt(harnessOf(specOf(), modelOf(steps), state), { run_id: RUN, run_kind: "hunt", spec: specOf(), actions: [], queue: new InProcessDirectiveQueue() });
    return state.read(RUN);
  }

  it("carries each turn's model cost and time once, on its own move", async () => {
    const events = (await hunted([
      () => ({ tool: "search_findings", args: { query: "45.77.53.176" } }),
      () => ({ tool: "search_findings", args: { query: "10.0.0.5" } }),
    ])) as { kind: string; payload: unknown }[];
    // Priced as a gateway would price it: the agent's own model calls, $0.0015 each.
    const priced = events.map((event) =>
      event.kind === "spend" && (event.payload as { role: string }).role === "agent" ? { ...event, payload: { ...(event.payload as object), cost_usd: 0.0015 } } : event,
    );
    const view = huntView(RUN, priced as never);
    expect(view.moves.map((move) => move.cost_usd)).toEqual([0.0015, 0.0015]);
    expect(view.moves.every((move) => typeof move.duration_ms === "number")).toBe(true);
    expect(view.calls.map((one) => one.cost_usd)).toEqual([0.0015, 0.0015]);
    // The replay reads a decision per move, keyed the same way.
    const replay = huntKernelReplay(RUN, priced as never);
    expect(replay.decisions.map((one) => one.decision_id)).toEqual(view.moves.map((move) => move.decision_id).reverse());
    expect(replay.decisions.map((one) => one.cost_usd)).toEqual([0.0015, 0.0015]);
    expect(replay.decisions.map((one) => one.recorded.iteration)).toEqual([1, 2]);
  });
});
