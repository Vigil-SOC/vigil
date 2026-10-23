import { describe, expect, it } from "vitest";
import { rareSweep } from "../../../plugins/rootcause/v3/rare.js";
import { MemoryTrace } from "../../../plugins/rootcause/v3/trace.js";
import { Registry, type EvidenceTool, type ToolResult } from "../../../plugins/rootcause/v3/tools/registry.js";

const range: [string, string] = ["2026-05-05T00:00:00Z", "2026-05-06T00:00:00Z"];
const line = (o: Record<string, unknown>) => JSON.stringify(o);

// A scripted Splunk: answers by the SPL, takes a different time per search (so they finish out of order), and
// counts how many run at once. edr has 3 usable fields (one refused by quota), cloud has 1, k8s's field search fails.
function scripted(opts: { serial?: boolean; onStart?: (n: number) => void } = {}) {
  let inFlight = 0, n = 0;
  const stats = { max: 0, started: 0 };
  let prev: Promise<unknown> = Promise.resolve();
  const answer = (spl: string): ToolResult => {
    if (spl.includes("| stats dc(")) {
      if (spl.includes('"k8s"')) return { ok: false, error: "search failed" };
      return { ok: true, data: line(spl.includes('"edr"') ? { ImageFileName: 12, CommandLine: 40, FileName: 1, action: 5, ComputerName: 3 } : { eventName: 7 }) };
    }
    if (spl.includes("CommandLine=*")) return { ok: false, error: "max concurrent searches reached" }; // Splunk quota
    const f = spl.match(/sourcetype="\w+" (\S+)=\*/)![1]!;
    return { ok: true, data: [1, 2].map((i) => line({ [f]: `${f}-v${i}`, count: String(i), first: "1777939200", last: "1777942800", ...(spl.includes("hosts") ? { hosts: "3", h: "a,b" } : {}) })).join("\n") };
  };
  const tool: EvidenceTool = {
    name: "splunk_query", description: "", inputSchema: { type: "object" }, readOnly: true,
    async run(input: { spl: string }) {
      const mine = prev;
      let release!: () => void;
      if (opts.serial) { const p = new Promise<void>((r) => (release = r)); prev = p; await mine; }
      stats.started++; opts.onStart?.(stats.started);
      stats.max = Math.max(stats.max, ++inFlight);
      await new Promise((r) => setTimeout(r, [9, 1, 6, 2, 4, 3][n++ % 6]));
      inFlight--;
      release?.();
      return answer(input.spl);
    },
  };
  return { tool, stats };
}

async function sweep(opts: { serial?: boolean; signal?: AbortSignal; onStart?: (n: number) => void } = {}) {
  const { tool, stats } = scripted(opts);
  const ledger: string[] = [];
  const tools = new Registry(5, async (c) => { ledger.push(c.toolCallId); }).register(tool); // 5 calls already on the ledger
  const trace = new MemoryTrace("t");
  const text = await rareSweep({ tools, trace: trace.forTurn(0, undefined, "rare"), index: "lab", range, domains: ["edr", "k8s", "cloud"], timeoutMs: 1000, signal: opts.signal }).catch((e) => e as Error);
  const traced = trace.chronological().filter((e) => e.type === "query").map((e) => (e.payload as { toolCallId: string }).toolCallId);
  return { text, stats, ledger, traced, tools };
}

describe("rare sweep", () => {
  it("runs at most 4 searches at once, with the same text, refs and ledger order as one at a time", async () => {
    const serial = await sweep({ serial: true });
    const live = await sweep();
    expect(serial.stats.max).toBe(1);
    expect(live.stats.max).toBeGreaterThan(1);
    expect(live.stats.max).toBeLessThanOrEqual(4);
    expect(live.text).toBe(serial.text);
    // edr: fields r6, its 3 field searches r7-r9 (r8 refused); k8s: r10 failed; cloud: r11, r12.
    expect(live.text).toContain("- edr:\n  ImageFileName (12 distinct) [r7]: ImageFileName-v1 ×1");
    expect(live.text).not.toContain("CommandLine");
    expect(live.text).toContain("  action (5 distinct) [r9]");
    expect(live.text).toContain("- k8s: sweep failed [r10]");
    expect(live.text).toContain("- cloud:\n  eventName (7 distinct) [r12]");
    const refs = ["r6", "r7", "r8", "r9", "r10", "r11", "r12"];
    expect(live.ledger).toEqual(refs);
    expect(live.traced).toEqual(refs);
    expect(live.tools.callCount).toBe(12);
  });

  it("starts no search after a stop, and returns only once the started ones are journaled", async () => {
    const stop = new AbortController();
    const r = await sweep({ signal: stop.signal, onStart: (n) => { if (n === 1) stop.abort(new Error("stopped")); } });
    expect(r.text).toBeInstanceOf(Error);
    expect(r.stats.started).toBe(1); // the fields search ran out; its field searches never started
    expect(r.ledger).toEqual(["r6"]);
  });

  it("stopped mid-sweep, leaves no queued search running and no ref unused", async () => {
    const stop = new AbortController();
    const r = await sweep({ signal: stop.signal, onStart: (n) => { if (n === 2) stop.abort(new Error("stopped")); } });
    expect(r.text).toBeInstanceOf(Error);
    expect(r.stats.started).toBeLessThan(7);
    expect(r.ledger).toEqual(Array.from({ length: r.stats.started }, (_, i) => `r${i + 6}`));
    expect(r.traced).toEqual(r.ledger);
  });
});
