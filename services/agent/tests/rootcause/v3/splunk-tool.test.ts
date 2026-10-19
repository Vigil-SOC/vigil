import { describe, expect, it } from "vitest";
import { PREFLIGHT_MAX, scopeSpl, searchWindow, splunkTool, validateSpl } from "../../../plugins/rootcause/v3/tools/splunk-tool.js";
import type { QueryInput, SearchBackend } from "../../../plugins/rootcause/v3/tools/search.js";

const range: [string, string] = ["2026-05-05T00:00:00Z", "2026-05-06T00:00:00Z"];

describe("validateSpl", () => {
  it("requires the index, no wildcards, and a sourcetype", () => {
    expect(validateSpl('sourcetype="a" x=1', "lab")).toMatch(/name the index/);
    expect(validateSpl('index=* sourcetype="a"', "lab")).toMatch(/only index=lab/);
    expect(validateSpl("index=lab x=1", "lab")).toMatch(/sourcetype/);
    expect(validateSpl('index=lab sourcetype="a" x=1 | stats count by user', "lab")).toBeNull();
    expect(validateSpl('| tstats count where index=lab sourcetype="a" by host', "lab")).toBeNull();
  });
  it("keeps the window out of the SPL and blocks expensive commands", () => {
    expect(validateSpl('index=lab sourcetype="a" earliest=-30d', "lab")).toMatch(/time window/);
    expect(validateSpl('index=lab sourcetype="a" | stats earliest(_time) as t by u', "lab")).toBeNull();
    expect(validateSpl('index=lab sourcetype="a" | transaction user', "lab")).toMatch(/expensive/);
    expect(validateSpl('index=lab sourcetype="a" [search index=lab sourcetype="b" x=1]', "lab")).toMatch(/bounded/);
    expect(validateSpl('index=lab sourcetype="a" [search index=lab sourcetype="b" | head 5 | fields u]', "lab")).toBeNull();
  });
});

describe("searchWindow", () => {
  it("defaults around the target time, clipped to the data", () => {
    expect(searchWindow({}, { range, focusTime: "2026-05-05T10:00:00Z" })).toMatchObject({ earliest: "2026-05-05T04:00:00.000Z", latest: "2026-05-05T11:00:00.000Z" });
    expect(searchWindow({}, { range, focusTime: "2026-05-05T02:00:00Z" }).earliest).toBe("2026-05-05T00:00:00.000Z");
    expect(searchWindow({}, { range })).toMatchObject({ earliest: "2026-05-05T00:00:00.000Z", latest: "2026-05-06T00:00:00.000Z" });
    expect(searchWindow({ earliest: "2026-05-05T01:00:00Z" }, { range, focusTime: "2026-05-05T10:00:00Z" }).note).toBe("as requested");
  });
});

describe("scopeSpl", () => {
  it("adds the allowed sourcetypes, refuses others", () => {
    expect(scopeSpl("index=lab x=1", ["a"], ["a", "b"])).toEqual({ spl: 'index=lab (sourcetype="a") x=1' });
    expect(scopeSpl('index=lab sourcetype="b"', ["a"], ["a", "b"])).toHaveProperty("refused");
    expect(scopeSpl("index=lab x=1", ["a", "b"], ["a", "b"])).toEqual({ spl: "index=lab x=1" });
  });
});

describe("splunkTool", () => {
  const ctx = { allowedDomains: ["a"], allDomains: ["a"], timeoutMs: 1000, range, focusTime: "2026-05-05T10:00:00Z" };
  const backend = (n: number): SearchBackend & { seen: QueryInput[] } => {
    const seen: QueryInput[] = [];
    return { seen, count: async () => n, search: async (q) => { seen.push(q); return q.spl.includes("fieldsummary") ? "field user values a,b" : n ? `${n} events matched, ${n} results (showing 1 of ${n})\n{}` : "0 events matched, 0 results (showing 0 of 0)"; } };
  };
  it("refuses a big raw search with top values", async () => {
    const r = await splunkTool(backend(PREFLIGHT_MAX + 1), "lab").run({ purpose: "find who logged on; expect one user", spl: 'index=lab sourcetype="a"' }, ctx);
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/over the 500 limit[\s\S]*field user/);
  });
  it("skips the preflight for a reporting search and sets the window as a parameter", async () => {
    const b = backend(10_000);
    const r = await splunkTool(b, "lab").run({ purpose: "count logons per user; expect one outlier", spl: 'index=lab sourcetype="a" | stats count by user' }, ctx);
    expect(r.ok).toBe(true);
    expect(b.seen[0]).toMatchObject({ earliest: "2026-05-05T04:00:00.000Z", latest: "2026-05-05T11:00:00.000Z" });
    expect(r.data).toMatch(/^purpose: count logons/);
  });
  it("marks an empty result as evidence against the expectation", async () => {
    const r = await splunkTool(backend(0), "lab").run({ purpose: "confirm the delete; expect 1 event", spl: 'index=lab sourcetype="a" x=1' }, ctx);
    expect(r.data).toMatch(/nothing matched/);
  });
});

describe("mapping mode", () => {
  const ctx = { allowedDomains: ["a"], allDomains: ["a"], timeoutMs: 1000, range, minCount: 10 };
  const seen: string[] = [];
  const b: SearchBackend = { count: async () => 1, search: async (q) => { seen.push(q.spl); return "3 events matched, 1 results (showing 1 of 1)\n{}"; } };
  it("cuts rows seen fewer than 10 times, in code", async () => {
    const r = await splunkTool(b, "lab").run({ purpose: "map users per host", spl: 'index=lab sourcetype="a" | stats count by host user | sort - count' }, ctx);
    expect(r.ok).toBe(true);
    expect(seen.at(-1)).toMatch(/\| stats count by host user \| sort - count \| where count >= 10$/);
    expect(r.data).toMatch(/mapping mode: rows seen fewer than 10 times are left out/);
  });
  it("refuses raw searches and renamed counts", async () => {
    expect((await splunkTool(b, "lab").run({ purpose: "raw events of a host", spl: 'index=lab sourcetype="a" host=x' }, ctx)).ok).toBe(false);
    expect((await splunkTool(b, "lab").run({ purpose: "renamed count column", spl: 'index=lab sourcetype="a" | stats count as n by host' }, ctx)).ok).toBe(false);
  });
});

describe("environment map wording", () => {
  it("is sent back when it judges activity", async () => {
    const { check } = await import("../../../plugins/rootcause/v3/model/client.js");
    const map = "**Systems**\n- " + "host WKS-014 seen 120 times. ".repeat(8);
    expect(check("submit_environment", { map, reasoning: "" }, 0).success).toBe(true);
    const r = check("submit_environment", { map: map + "\n- one outlier connection to 198.51.100.77, suspicious", reasoning: "" }, 0);
    expect(r.success).toBe(false);
    expect((r as { msg: string }).msg).toMatch(/outlier, suspicious/);
  });
});

describe("mapping mode destinations", () => {
  it("refuses grouping by a destination address", async () => {
    const b: SearchBackend = { count: async () => 1, search: async () => "1 events matched, 1 results\n{}" };
    const ctx = { allowedDomains: ["a"], allDomains: ["a"], timeoutMs: 1000, range, minCount: 10 };
    expect((await splunkTool(b, "lab").run({ purpose: "map connections", spl: 'index=lab sourcetype="a" | stats count by id_orig_h id_resp_h' }, ctx)).ok).toBe(false);
    expect((await splunkTool(b, "lab").run({ purpose: "map EDR network", spl: 'index=lab sourcetype="a" | stats count by ComputerName RemoteAddressIP4' }, ctx)).ok).toBe(false);
    expect((await splunkTool(b, "lab").run({ purpose: "map sources", spl: 'index=lab sourcetype="a" | stats count by id_orig_h' }, ctx)).ok).toBe(true);
  });
});
