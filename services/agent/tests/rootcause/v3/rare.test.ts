import { describe, expect, it } from "vitest";
import { rareSweep } from "../../../plugins/rootcause/v3/rare.js";
import { Registry } from "../../../plugins/rootcause/v3/tools/registry.js";
import type { TurnTrace } from "../../../plugins/rootcause/v3/trace.js";

const range: [string, string] = ["2026-05-05T00:00:00Z", "2026-05-06T00:00:00Z"];
const domains = ["A", "B", "C"];
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// A: three usable fields. B: its overview search fails. C: two usable fields, one of which comes back empty.
const DC: Record<string, Record<string, string>> = { A: { Image: "5", CommandLine: "9", verb: "4", host: "2" }, C: { Image: "6", action: "3", host: "1" } };

async function sweep(opts: { delay: (n: number) => number; signal?: AbortSignal; onStart?: (n: number) => void }) {
  const journal: string[] = [];
  const spls: string[] = [];
  const state = { inFlight: 0, max: 0 };
  const registry = new Registry(0, async (c) => { journal.push(c.toolCallId); });
  registry.register({
    name: "splunk_query", description: "", inputSchema: { type: "object" }, readOnly: true,
    async run(input: { spl: string }) {
      const n = spls.push(input.spl);
      state.max = Math.max(state.max, ++state.inFlight);
      opts.onStart?.(n);
      await sleep(opts.delay(n));
      state.inFlight--;
      const st = /sourcetype="(\w)"/.exec(input.spl)![1]!;
      if (st === "B") return { ok: false, error: "refused" };
      if (input.spl.includes("| stats dc(")) return { ok: true, data: JSON.stringify(DC[st]) };
      const f = /^\S+ \S+ '?(\w+)'?=\*/.exec(input.spl)![1]!;
      if (st === "C" && f === "action") return { ok: true, data: "" };
      return { ok: true, data: `{"${f}":"v-${st}-${f}","count":"1","first":"1778000000","last":"1778000000","hosts":"1","h":"h1"}` };
    },
  });
  const entries: { type: string; payload: any }[] = [];
  const trace: TurnTrace = { write: (type, payload) => (entries.push({ type, payload }), "T"), relate: () => {} };
  const text = await rareSweep({ tools: registry, trace, index: "lab", range, domains, timeoutMs: 1000, ...(opts.signal ? { signal: opts.signal } : {}) });
  return { text, journal, entries, max: state.max, started: spls.length, registry };
}

describe("rareSweep", () => {
  it("runs searches concurrently under the cap, with the text, refs, ledger and trace of a sequential sweep", async () => {
    const ref = await sweep({ delay: () => 0 });
    // Out of order: later searches finish first.
    const run = await sweep({ delay: (n) => 40 - n * 4 });
    expect(run.max).toBeLessThanOrEqual(4);
    expect(run.max).toBeGreaterThan(1);
    expect(run.text).toBe(ref.text);
    // Sequential order: A dc r1, A fields r2-r4, B dc r5, C dc r6, C fields r7-r8.
    expect(run.text).toMatch(/- A:\n {2}Image \(5 distinct\) \[r2\].*\n {2}CommandLine \(9 distinct\) \[r3\].*\n {2}verb \(4 distinct\) \[r4\]/);
    expect(run.text).toContain("- B: sweep failed [r5]");
    expect(run.text).toMatch(/- C:\n {2}Image \(6 distinct\) \[r7\]/);
    expect(run.text).not.toContain("action (");
    const refs = ["r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8"];
    expect(run.journal).toEqual(refs);
    expect(run.registry.callCount).toBe(8);
    expect(run.entries.filter((e) => e.type === "query").map((e) => e.payload.toolCallId)).toEqual(refs);
    expect(run.entries.filter((e) => e.type === "tool-output").map((e) => e.payload.toolCallId)).toEqual(refs);
    expect(run.entries).toEqual(ref.entries);
  });

  it("starts no new search once stopped", async () => {
    const ctl = new AbortController();
    let started = 0;
    const stopped = sweep({ delay: () => 10, signal: ctl.signal, onStart: (n) => { started = n; if (n === 3) ctl.abort(); } });
    await expect(stopped).rejects.toThrow();
    expect(started).toBe(3); // nothing queued behind the stop began
  });
});
