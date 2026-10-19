import { describe, expect, it } from "vitest";
import type { ToolResult } from "../../../contracts/tool.js";
import { LiveSearch } from "../../../plugins/rootcause/v3/tools/search.js";
import { sourceFor, UnsupportedSource, type Invoke } from "../../../plugins/rootcause/v3/tools/source.js";
import { SPLUNK_EXECUTE, splunkAdapter, splunkRunner } from "../../../plugins/rootcause/v3/tools/splunk-source.js";

// splunk_execute's answer as the tools router delivers it: the whole envelope as one row.
// Shape taken from a live call through /internal/tools/invoke; values made up.
const envelope = (results: Record<string, unknown>[], extra: Record<string, unknown> = {}): ToolResult => ({
  ok: true,
  rows: [{ success: true, query: "q", count: results.length, results, ...extra }],
  rowCount: 1,
  capped: false,
  sourceSystem: "splunk-selfhosted",
});

function recording(answers: ToolResult[]): { invoke: Invoke; asked: Record<string, unknown>[] } {
  const asked: Record<string, unknown>[] = [];
  return {
    asked,
    invoke: async (args) => {
      asked.push(args);
      const next = answers.shift();
      if (next === undefined) throw new Error("no more answers");
      return next;
    },
  };
}

const range: [string, string] = ["2026-01-05T00:00:00Z", "2026-01-06T00:00:00Z"];
const window = { earliest: Date.parse(range[0]) / 1000, latest: Date.parse(range[1]) / 1000 };

describe("splunkRunner", () => {
  it("asks splunk_execute for the search, its window in epoch seconds and its row limit", async () => {
    const { invoke, asked } = recording([envelope([])]);
    await splunkRunner(invoke).runJob("index=lab sourcetype=edr | stats count", { ...window, limit: 50 });
    expect(asked).toEqual([{ spl_query: "index=lab sourcetype=edr | stats count", earliest: "1767571200", latest: "1767657600", max_results: 50 }]);
  });

  it("reads the rows, the events matched and Splunk's warnings out of the envelope", async () => {
    const { invoke } = recording([envelope([{ host: "h1", count: "7" }, { host: "h2", count: "3" }], { event_count: 48211, warnings: ["field 'usr' not found"] })]);
    const job = await splunkRunner(invoke).runJob("index=lab | stats count by host", window);
    expect(job).toMatchObject({ rows: [{ host: "h1", count: "7" }, { host: "h2", count: "3" }], events: 48211, results: 2, errors: [], warnings: ["field 'usr' not found"], finalized: false });
  });

  it("counts what came back when the server reports no event_count", async () => {
    const { invoke } = recording([envelope([{ _raw: "a" }, { _raw: "b" }])]);
    expect((await splunkRunner(invoke).runJob("index=lab", window)).events).toBe(2);
  });

  it("hands Splunk's own reason to the model as an error", async () => {
    const { invoke } = recording([{ ok: false, failure: { kind: "backend_error", detail: "Splunk: Unknown search command 'statz'." } }]);
    const job = await splunkRunner(invoke).runJob("index=lab | statz", window);
    expect(job.errors).toEqual(["Splunk: Unknown search command 'statz'."]);
    expect(job.rows).toEqual([]);
  });

  it("marks a search that ran out of time as partial, not as an error", async () => {
    const { invoke } = recording([{ ok: false, failure: { kind: "timeout", timeoutMs: 120_000 } }]);
    expect(await splunkRunner(invoke).runJob("index=lab", window)).toMatchObject({ finalized: true, errors: [] });
  });

  it("gives the loop's search tool the true match count, not the rows it was shown", async () => {
    const { invoke } = recording([envelope([{ host: "h1", count: "9" }], { event_count: 48211 })]);
    const out = await new LiveSearch(splunkRunner(invoke), "lab", range).search({ spl: "index=lab sourcetype=edr | stats count by host", earliest: range[0], latest: range[1] });
    expect(out.split("\n")[0]).toBe("48,211 events matched, 1 results (showing 1 of 1)");
  });
});

describe("splunkAdapter", () => {
  it("surveys the index: every sourcetype with its size and real fields, one domain each", async () => {
    const { invoke, asked } = recording([
      envelope([
        { sourcetype: "edr", count: "1200", first: String(window.earliest), last: String(window.latest) },
        { sourcetype: "auth", count: "90", first: String(window.earliest), last: String(window.latest) },
      ]),
      envelope([{ field: "host" }, { field: "process" }, { field: "_time" }]),
      envelope([{ field: "user" }]),
    ]);
    const survey = await splunkAdapter.open(invoke, { index: "lab", range }).survey();
    expect(asked[0]!["spl_query"]).toContain("| tstats count min(_time) as first max(_time) as last where index=lab by sourcetype");
    expect(survey.domains).toEqual(["edr", "auth"]);
    expect(survey.sizes).toEqual({ edr: 1200, auth: 90 });
    expect(survey.text).toContain("fields: host, process");
  });

  it("refuses to survey on a failed search rather than describe an empty environment", async () => {
    const { invoke } = recording([{ ok: false, failure: { kind: "backend_error", detail: "Splunk: index lab not found" } }]);
    await expect(splunkAdapter.open(invoke, { index: "lab", range }).survey()).rejects.toThrow("index lab not found");
  });

  it("searches with v3's guarded tool, scoped to the run's index", () => {
    const tool = splunkAdapter.open(async () => envelope([]), { index: "lab", range }).evidenceTool();
    expect(tool.name).toBe("splunk_query");
    expect(tool.description).toContain("index=lab");
  });
});

describe("sourceFor", () => {
  it("picks the adapter for the telemetry tool the run was bound to", () => {
    expect(sourceFor([splunkAdapter], ["indicator_lookup", SPLUNK_EXECUTE])).toEqual({ adapter: splunkAdapter, tool: SPLUNK_EXECUTE });
  });

  it("refuses a binding no adapter reads, naming what is bound", () => {
    expect(() => sourceFor([splunkAdapter], ["elastic_elastic_search_logs"])).toThrow(UnsupportedSource);
    expect(() => sourceFor([splunkAdapter], ["elastic_elastic_search_logs"])).toThrow(/reads splunk-selfhosted so far.*elastic_elastic_search_logs/);
  });
});
