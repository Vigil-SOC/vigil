import { describe, expect, it } from "vitest";
import type { ToolResult } from "../../contracts/tool.js";
import { SHOWN_EVENTS, shown as shownAt } from "../../workflows/rootcause/trim.js";

const CAP = 20_000;
const shown = (result: ToolResult) => shownAt(result, CAP);

function envelope(results: unknown[]): ToolResult {
  const row = { success: true, query: "index=botsv3 x", count: results.length, results };
  return { ok: true, rows: [row], rowCount: 1, capped: false, sourceSystem: "splunk" };
}

function event(n: number): Record<string, unknown> {
  return {
    _bkt: "botsv3~305~3F275426",
    _cd: `305:${n}`,
    _indextime: "1534759301",
    _raw: `2018-08-20 11:39:51,Info,MKRAEUS-L,event ${n}`,
    _serial: String(n),
    _si: ["splunk", "botsv3"],
    _sourcetype: "symantec:ep",
    _subsecond: ".123",
    _time: "2018-08-20T11:39:51.000+00:00",
    host: "SEPM",
    index: "botsv3",
    linecount: "1",
    punct: "--_::,,-,:_,",
    source: "symantec.log",
    sourcetype: "symantec:ep",
    splunk_server: "splunk",
  };
}

function inner(result: ToolResult): Record<string, unknown>[] {
  if (!result.ok) throw new Error("expected ok");
  return (result.rows[0] as { results: Record<string, unknown>[] }).results;
}

describe("what a search shows the investigator", () => {
  it("keeps the first events of a splunk envelope and the envelope's real count", () => {
    const result = shown(envelope(Array.from({ length: 129 }, (_, n) => event(n))));
    expect(inner(result)).toHaveLength(SHOWN_EVENTS);
    if (!result.ok) throw new Error("expected ok");
    expect(result.rows[0]).toMatchObject({ count: 129, query: "index=botsv3 x" });
    expect(result.capped).toBe(true);
  });

  it("drops splunk's internal fields and keeps the event's own", () => {
    const [first] = inner(shown(envelope([{ ...event(1), file: "BRUCE BIRTHDAY HAPPY HOUR PICS.lnk", user: "bgist" }])));
    expect(Object.keys(first!).sort()).toEqual(["_time", "file", "host", "index", "source", "sourcetype", "user"]);
  });

  it("keeps _raw when the event has few fields of its own", () => {
    const [first] = inner(shown(envelope([{ _raw: "a line", _time: "t", _cd: "1", host: "h" }])));
    expect(first).toEqual({ _raw: "a line", _time: "t", host: "h" });
  });

  it("cuts a long value and says so", () => {
    const [first] = inner(shown(envelope([{ _raw: "x".repeat(5_000), host: "h" }])));
    const raw = String(first!["_raw"]);
    expect(raw.length).toBeLessThan(1_100);
    expect(raw.endsWith("[cut]")).toBe(true);
  });

  it("drops events off the end until the result fits the cap, so nothing is clamped", () => {
    const wide = (n: number) => Object.fromEntries(Array.from({ length: 12 }, (_, f) => [`field${f}`, `${n}-${"v".repeat(900)}`]));
    const result = shown(envelope(Array.from({ length: 25 }, (_, n) => wide(n))));
    if (!result.ok) throw new Error("expected ok");
    const kept = inner(result);
    expect(kept.length).toBeGreaterThan(0);
    expect(kept.length).toBeLessThan(25);
    expect(JSON.stringify(result.rows, null, 2).length).toBeLessThanOrEqual(CAP);
    expect(kept[0]!["field0"]).toMatch(/^0-/);
    expect(result.rows[0]).toMatchObject({ count: 25 });
    expect(result.capped).toBe(true);
  });

  it("keeps the first event even when it alone is over the cap", () => {
    const huge = Object.fromEntries(Array.from({ length: 40 }, (_, f) => [`field${f}`, "v".repeat(900)]));
    expect(inner(shown(envelope([huge, huge])))).toHaveLength(1);
  });

  it("leaves a stats row, an error row, and a non-splunk result alone", () => {
    const stats = envelope([{ count: "0" }]);
    expect(shown(stats)).toEqual(stats);
    const error: ToolResult = { ok: true, rows: [{ error: "Splunk search failed or timed out", query: "x" }], rowCount: 1, capped: false, sourceSystem: "splunk" };
    expect(shown(error)).toEqual(error);
    const plain: ToolResult = { ok: true, rows: [{ _id: "1", message: "m" }], rowCount: 1, capped: false, sourceSystem: "elastic" };
    expect(shown(plain)).toEqual(plain);
    const failed: ToolResult = { ok: false, failure: { kind: "unavailable", detail: "down" } };
    expect(shown(failed)).toEqual(failed);
  });
});
