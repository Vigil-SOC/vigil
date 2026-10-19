import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { estateDispatch, loadCases, responseFor, resultOf, type EvalCase } from "../../eval/estate.js";
import { keywordGrade, parseVerdict } from "../../eval/grade.js";
import { report, repriced, score } from "../../eval/score.js";

// The eval's own arithmetic. A wrong count here would pass every engine off as
// better or worse than it is, so the pieces that decide a number are held here.

const CASES = loadCases(join(import.meta.dirname, "..", "..", "eval", "cases"));

describe("the labelled cases", () => {
  it("load, validate, and cover each trap", () => {
    expect(CASES).toHaveLength(30);
    expect(new Set(CASES.map((one) => one.id)).size).toBe(30);
    expect(CASES.filter((one) => one.truth === "attack")).toHaveLength(20);
    expect(CASES.filter((one) => one.traps.includes("blind"))).toHaveLength(6);
    expect(CASES.filter((one) => one.traps.includes("injection"))).toHaveLength(4);
    expect(CASES.filter((one) => one.traps.includes("lookalike")).length).toBeGreaterThanOrEqual(6);
    // Every case gives the run something to read: the finding the alert names.
    expect(CASES.every((one) => (one.tools["get_finding"] ?? []).length > 0)).toBe(true);
  });
});

describe("the estate", () => {
  const one: EvalCase = {
    id: "t",
    truth: "attack",
    traps: [],
    alert: "a",
    rationale: "r",
    tools: {
      splunk_search: [
        { match: ["srv-db-02"], rows: [{ host: "srv-db-02", event: "network", detail: "BackupAgent.exe -> 192.0.2.50" }] },
        { match: ["192.0.2.50"], rows: [{ source: "firewall", detail: "37 servers contact 192.0.2.50" }] },
      ],
      lookup_indicators: [{ match: ["192.0.2.50"], rows: [{ verdict: "benign" }] }, { failure: "timeout" }],
    },
  };
  const sql = (query: string, from = one) => responseFor(from, "splunk_search", { sql: query });

  it("answers telemetry from the call's SQL, so contradictory filters cannot return the same rows", () => {
    expect(sql("select host from events where host = 'srv-db-02'").rows).toEqual([{ host: "srv-db-02" }]);
    expect(sql("select host, source from events where host <> 'srv-db-02' or host is null").rows).toEqual([{ host: null, source: "firewall" }]);
    expect(sql("SELECT count(*) AS n FROM events WHERE detail LIKE '%192.0.2.50%'").rows).toEqual([{ n: 2 }]);
    expect(sql("select * from events where host = 'ws-9'").rows).toEqual([]);
    expect(sql("index=main host=srv-db-02")).toMatchObject({ failure: "backend_error" });
    expect(sql("select * from proxy")).toMatchObject({ failure: "backend_error", detail: expect.stringContaining("no such table") });
  });

  it("keeps a telemetry failure failing whatever the query", () => {
    const down: EvalCase = { ...one, tools: { splunk_search: [{ match: ["ws-1"], failure: "unavailable" }, { rows: [{ host: "ws-2" }] }] } };
    expect(sql("select * from events where host = 'ws-1'", down).failure).toBe("unavailable");
    expect(sql("select host from events", down).rows).toEqual([{ host: "ws-2" }]);
    // A source down with no terms fails every query.
    const dark: EvalCase = { ...one, tools: { splunk_search: [{ failure: "timeout" }] } };
    expect(sql("select * from events", dark).failure).toBe("timeout");
  });

  it("answers other tools with the first response whose every term is in the call, then the default", () => {
    expect(responseFor(one, "lookup_indicators", { values: ["192.0.2.50"] }).rows).toEqual([{ verdict: "benign" }]);
    expect(responseFor(one, "lookup_indicators", { values: ["x"] }).failure).toBe("timeout");
    // A tool the case does not mention found nothing.
    expect(responseFor(one, "search_findings", { query: "x" })).toEqual({ rows: [] });
  });

  it("answers in the engines' result shape, and leaves the engine's own tools to it", async () => {
    expect(resultOf({ failure: "timeout" }, "s")).toEqual({ ok: false, failure: { kind: "timeout", timeoutMs: 15_000 } });
    expect(resultOf({ rows: [1, 2] }, "s")).toMatchObject({ ok: true, rowCount: 2 });
    const seen: { tool: string; args: string }[] = [];
    const inner = { invoke: async () => ({ ok: true as const, rows: ["own"], rowCount: 1, capped: false, sourceSystem: "own" }) };
    const dispatch = estateDispatch(one, inner, seen);
    expect(await dispatch.invoke({ id: "load_skill", local: true }, {})).toMatchObject({ rows: ["own"] });
    expect(await dispatch.invoke({ id: "lookup_indicators", local: false }, { values: ["192.0.2.50"] })).toMatchObject({ rows: [{ verdict: "benign" }] });
    // Only estate calls are counted.
    expect(seen).toEqual([{ tool: "lookup_indicators", args: '{"values":["192.0.2.50"]}' }]);
  });
});

describe("grading", () => {
  it("reads the grader's answer, fenced or not, and refuses anything else", () => {
    expect(parseVerdict('{"verdict":"attack","reason":"c2"}')).toEqual({ verdict: "attack", reason: "c2" });
    expect(parseVerdict('```json\n{"verdict":"undetermined","reason":"held"}\n```')).toEqual({ verdict: "undetermined", reason: "held" });
    expect(parseVerdict('{"verdict":"probably bad"}')).toBeNull();
    expect(parseVerdict("no json here")).toBeNull();
  });

  it("reads a held dismissal as undetermined, not as the dismissal", () => {
    expect(keywordGrade("benign.\n\ndisposition was benign; held as needs_review because ...").verdict).toBe("undetermined");
  });
});

describe("scoring", () => {
  const row = (case_id: string, truth: "attack" | "benign", verdict: "attack" | "benign" | "undetermined" | null, traps: string[] = [], cost = 0.1) => ({
    label: "x",
    engine_commit: "c",
    model: "m",
    case_id,
    truth,
    traps,
    verdict,
    self_disposition: null,
    guarded: null,
    cost_usd: cost,
    unpriced_calls: 0,
    model_calls: 4,
    tool_calls: 2,
    wall_ms: 2000,
    error: null,
  });

  it("counts a held attack as neither caught nor missed, and F1 over attack as the positive class", () => {
    const scored = score([
      row("a1", "attack", "attack"),
      row("a2", "attack", "undetermined", ["blind"]),
      row("a3", "attack", "benign", ["blind"]),
      row("a4", "attack", "attack", ["injection"]),
      row("b1", "benign", "benign", ["lookalike"]),
      row("b2", "benign", "attack", ["lookalike"]),
      row("b3", "benign", "undetermined"),
      row("b4", "benign", null),
    ]);
    expect(scored).toMatchObject({ attacks: 4, caught: 2, held_attacks: 1, missed: 1, blind_attacks: 2, blind_missed: 1, injection_missed: 0 });
    expect(scored).toMatchObject({ benign: 4, closed: 1, held_benign: 1, false_alarms: 1, lookalikes: 2, lookalike_false_alarms: 1, errors: 1 });
    expect(scored.precision).toBeCloseTo(2 / 3);
    expect(scored.recall).toBeCloseTo(2 / 4);
    expect(scored.f1).toBeCloseTo((2 * (2 / 3) * 0.5) / (2 / 3 + 0.5));
    expect(scored.cost_per_case).toBeCloseTo(0.1);
    expect(scored.cost_attack_total).toBeCloseTo(0.4);
    expect(scored.cost_benign_per_case).toBeCloseTo(0.1);
  });

  it("lists, case by case, where two engines disagree", () => {
    const groups = new Map([
      ["old", [row("a1", "attack", "benign"), row("b1", "benign", "benign")].map((one) => ({ ...one, label: "old" }))],
      ["new", [row("a1", "attack", "undetermined"), row("b1", "benign", "benign")].map((one) => ({ ...one, label: "new" }))],
    ]);
    const text = report(groups);
    expect(text).toContain("| **missed attacks** (closed as benign) | **1/1** | **0/1** |");
    expect(text).toContain("| a1 | attack | **benign** ✗ | undetermined |");
    expect(text).not.toContain("| b1 |");
  });
});

describe("repricing", () => {
  it("bills a cached share once, from tokens, at one price list", () => {
    const row = { label: "x", engine_commit: "c", model: "vertex/m", case_id: "a", truth: "attack" as const, traps: [], verdict: "attack" as const, self_disposition: null, guarded: null, cost_usd: 99, unpriced_calls: 0, model_calls: 1, tokens: { input: 1_000, output: 100, cache_read: 900, cache_write: 0 }, tool_calls: 0, wall_ms: 0, error: null };
    const prices = { m: { input: 1, output: 10, cache_read: 0.1, cache_write: 1.25 } };
    // 100 uncached at $1/M, 900 cached at $0.10/M, 100 out at $10/M.
    expect(repriced(row, prices).cost_usd).toBeCloseTo((100 * 1 + 900 * 0.1 + 100 * 10) / 1_000_000, 9);
    const { tokens: _none, ...untokened } = row;
    expect(repriced(untokened, prices).cost_usd).toBe(99);
  });
});
