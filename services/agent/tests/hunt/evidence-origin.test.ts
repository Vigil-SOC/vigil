// #2287: an evidence row names the system its rows came from -- the alerts it cites, or the
// integration that answered when it cites none -- rather than only the hunt's domain word.
import { describe, expect, it } from "vitest";
import type { CallPayload } from "../../contracts/events.js";
import type { ToolSpec } from "../../core/spec.js";
import { callsIn } from "../../core/agent.js";
import { InProcessState } from "../../core/state.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import { servedRefsSince, servedSince } from "../../plugins/hunt/hooks.js";
import type { HuntEvent, HuntKinds } from "../../plugins/hunt/ledger.js";
import { huntProjection } from "../../plugins/hunt/projection.js";
import { runHunt } from "../../plugins/hunt/workflow.js";
import { RUN, estate, finding, harnessOf, hypothesisIn, modelOf, specOf } from "../support/kernel-hunt.js";

const TOOLS: ToolSpec[] = [
  { id: "splunk-selfhosted_splunk_execute", kind: "remote", provides: "telemetry_search", server: "splunk-selfhosted" },
  { id: "virustotal_get_ip_report", kind: "remote", provides: "indicator_lookup", server: "virustotal" },
  { id: "search_findings", kind: "remote", provides: "findings_search" },
];

let at = 0;
const call = (tool: string, ok = true): CallPayload => ({ ref: `r${++at}`, turn: at, call_id: `c${at}`, tool, arguments: "{}", result: "", ok, failure: ok ? null : "down" });

describe("the integration a record's rows came from", () => {
  it("is the one server that answered every estate query since the last record", () => {
    const calls = [call("virustotal_get_ip_report"), call("record_evidence"), call("load_skill"), call("splunk-selfhosted_splunk_execute"), call("splunk-selfhosted_splunk_execute")];
    expect(servedSince(calls, TOOLS)).toBe("splunk-selfhosted");
  });

  it("is left unsaid when two servers answered, rather than guessed", () => {
    expect(servedSince([call("splunk-selfhosted_splunk_execute"), call("virustotal_get_ip_report")], TOOLS)).toBeNull();
  });

  it("is left unsaid when one of Vigil's own tools answered: its rows name their own source", () => {
    expect(servedSince([call("splunk-selfhosted_splunk_execute"), call("search_findings")], TOOLS)).toBeNull();
    expect(servedSince([call("search_findings")], TOOLS)).toBeNull();
  });

  it("counts no call that failed, and nothing before the hunt has queried", () => {
    expect(servedSince([call("virustotal_get_ip_report", false), call("splunk-selfhosted_splunk_execute")], TOOLS)).toBe("splunk-selfhosted");
    expect(servedSince([], TOOLS)).toBeNull();
  });
});

// #2520: a record rests on the searches whose results the model could have read when it wrote it.
describe("the searches a record rests on", () => {
  const at = (turn: number, ref: string, tool = "splunk-selfhosted_splunk_execute"): CallPayload => ({ ref, turn, call_id: ref, tool, arguments: "{}", result: "", ok: true, failure: null, class: "served" });
  const s1 = at(3, "r1");

  it("leaves out a search from the record's own batch, whose result the model had not seen", () => {
    expect(servedRefsSince([s1], 3)).toEqual([]);
  });

  it("keeps a search read on the next turn, after a record in its batch", () => {
    expect(servedRefsSince([s1, at(3, "r2", "record_evidence")], 4)).toEqual(["r1"]);
  });

  it("gives a second record in one batch nothing from that batch", () => {
    expect(servedRefsSince([at(2, "r1"), s1, at(3, "r3", "record_evidence")], 3)).toEqual([]);
  });

  it("counts from the start of the hunt before any record, and drops what came before the last one", () => {
    expect(servedRefsSince([at(1, "r1"), at(2, "r2")], 3)).toEqual(["r1", "r2"]);
    expect(servedRefsSince([at(1, "r1"), at(2, "r2", "record_evidence"), at(2, "r3"), at(3, "r4")], 4)).toEqual(["r3", "r4"]);
  });
});

describe("a hunt whose model searches and records in one response", () => {
  it("gives that record no ref to the search beside it, and the next record the search", async () => {
    const state = new InProcessState<HuntKinds>();
    const spec = specOf();
    const provider = modelOf([
      (request) => ({
        calls: [
          { tool: "splunk_search", args: { sql: "select * from flows where src = '10.0.0.5'" } },
          { tool: "record_evidence", args: { results: [finding("network", hypothesisIn(request))] } },
        ],
      }),
      (request) => ({ tool: "record_evidence", args: { results: [finding("endpoint", hypothesisIn(request))] } }),
      () => ({ say: "done" }),
    ]);
    await runHunt(harnessOf(spec, provider, state, estate()), { run_id: RUN, run_kind: "hunt", spec, actions: [], queue: new InProcessDirectiveQueue() });

    const held = await state.read(RUN);
    const search = callsIn(held).find((one) => one.tool === "splunk_search");
    const rows = held.filter((one) => one.kind === "evidence").map((one) => one.payload as { source_system?: string; call_refs?: string[] });
    expect(search).toBeDefined();
    expect(rows.find((row) => row.source_system === "network")?.call_refs).toEqual([]);
    expect(rows.find((row) => row.source_system === "endpoint")?.call_refs).toEqual([search?.ref]);
  });
});

describe("an evidence row a console is sent", () => {
  it("carries the alerts its rows cite and the integration that answered", async () => {
    const state = new InProcessState<HuntKinds>();
    const base = specOf();
    // The deployment's SIEM, named by the server the resolver bound it from.
    const spec = { ...base, tools: base.tools.map((tool) => (tool.id === "splunk_search" ? { ...tool, server: "splunk-selfhosted" } : tool)) };
    const cited = { ...finding("endpoint", "-"), payload: JSON.stringify({ rows: [{ finding_id: "dl-003", host: "FIN-WS-114" }] }) };
    const provider = modelOf([
      () => ({ tool: "splunk_search", args: { sql: "select * from flows where src = '10.0.0.5'" } }),
      (request) => ({ tool: "record_evidence", args: { results: [finding("network", hypothesisIn(request)), { ...cited, supports: [hypothesisIn(request)] }] } }),
      () => ({ say: "done" }),
    ]);
    await runHunt(harnessOf(spec, provider, state, estate()), { run_id: RUN, run_kind: "hunt", spec, actions: [], queue: new InProcessDirectiveQueue() });

    const rows = huntProjection(RUN, (await state.read(RUN)) as readonly HuntEvent[]).evidence.filter((row) => row.provenance === "worker");
    const network = rows.find((row) => row.source_system === "network");
    const endpoint = rows.find((row) => row.source_system === "endpoint");
    expect(network).toMatchObject({ finding_ids: [], served_by: "splunk-selfhosted" });
    expect(endpoint).toMatchObject({ finding_ids: ["dl-003"] });
  });
});
