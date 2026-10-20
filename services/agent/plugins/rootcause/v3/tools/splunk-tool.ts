// The one evidence tool: free SPL behind rules the tool enforces, not the prompt.
//  1. Scoped: the run's index (no wildcards), a sourcetype filter, and a time window the tool owns.
//     The window is a job parameter, never SPL; by default it is built around the move's target time, and
//     widening it means passing earliest/latest explicitly (traced with the purpose).
//  2. No expensive commands: transaction, map, unbounded subsearches.
//  3. Preflight: a search without a reporting command is counted first; over PREFLIGHT_MAX events it is
//     refused with the top values of its fields, so the model aggregates or narrows.
//  4. Output caps live in the backend (rows, chars, "showing N of M"); full output goes to the trace.
//  5. A stated purpose with the expected result, so an empty result is usable as refuting evidence.
import { z } from "zod4";
import type { Domain } from "../types.js";
import type { EvidenceTool, ToolContext } from "./registry.js";
import { REPORTING, type SearchBackend } from "./search.js";

export const PREFLIGHT_MAX = 500;
const BEFORE_MS = 6 * 3600_000, AFTER_MS = 3600_000;

const SplunkInput = z.object({
  purpose: z.string().min(10).describe("what this search should show and the result you expect, e.g. 'confirm process X on host Y wrote the file; expect 1 event'. An empty or different result then counts against that expectation."),
  spl: z.string().min(1).describe("SPL with index=, a sourcetype filter and field filters; aggregate with stats/tstats (count, values(), min/max(_time) by the identity or resource) before pulling raw events. No earliest=/latest= in the SPL."),
  earliest: z.string().optional().describe("ISO 8601 window start. Default: 6 hours before the target's time (48 hours when looking for a cause; the whole data range when the target has no time)."),
  latest: z.string().optional().describe("ISO 8601 window end. Default: 1 hour after the target's time (or the end of the data)."),
});
export type SplunkInputT = z.infer<typeof SplunkInput>;

const stMentions = (spl: string) => [
  ...[...spl.matchAll(/\bsourcetype\s*=\s*"?([^\s"|)]+)"?/gi)].map((m) => m[1]!),
  ...[...spl.matchAll(/\bsourcetype\s+IN\s*\(([^)]*)\)/gi)].flatMap((m) => m[1]!.split(",").map((s) => s.trim().replace(/^"|"$/g, ""))),
];

// Limits a search to the allowed sourcetypes: refuses one that names another, and adds the filter when it
// names none. With every domain allowed it passes through unchanged.
export function scopeSpl(spl: string, allowed: Domain[], all: Domain[]): { spl: string } | { refused: string } {
  if (!allowed.length || all.every((d) => allowed.includes(d))) return { spl };
  const named = stMentions(spl);
  const outside = named.filter((s) => !allowed.includes(s));
  if (outside.length) return { refused: `this move may search only ${allowed.join(", ")}; the search names ${outside.join(", ")}` };
  if (named.length) return { spl };
  if (/^\s*\|/.test(spl)) return { refused: `this move may search only ${allowed.join(", ")}; name the sourcetype in the search` };
  const list = allowed.map((a) => `sourcetype="${a}"`).join(" OR ");
  const m = spl.match(/^\s*(index\s*=\s*\S+)/i);
  return { spl: m ? `${m[1]} (${list}) ${spl.slice(m[0].length).trim()}`.trim() : `(${list}) ${spl}` };
}

// The rules every search must meet. Returns why it is refused, or null.
export function validateSpl(spl: string, index: string): string | null {
  const idx = [...spl.matchAll(/\bindex\s*=\s*"?([^\s"|)\]]+)"?/gi)].map((m) => m[1]);
  if (!idx.length) return `name the index: index=${index}`;
  const bad = idx.filter((i) => i !== index);
  if (bad.length) return `only index=${index} may be searched (got ${bad.join(", ")}); no wildcards`;
  if (!stMentions(spl).length) return "filter on a sourcetype (sourcetype=... or sourcetype IN (...))";
  if (/\b(earliest|latest)\s*=/i.test(spl.replace(/\b(earliest|latest)\(/gi, ""))) return "set the time window with the earliest/latest parameters, not in the SPL";
  if (/\|\s*(transaction|map|localize|gentimes|loadjob)\b/i.test(spl)) return "transaction, map and similar commands are too expensive; use stats ... by <id field> instead";
  for (const sub of spl.match(/\[[^\]]*\]/g) ?? []) {
    if (/^\[\s*\]$|^\[\d/.test(sub) || /\{\}/.test(sub)) continue; // not a subsearch (mvindex, field{})
    if (!/\|\s*(head|return|top|stats|dedup|tstats)\b/i.test(sub)) return "a subsearch must be bounded (end it with | head N, | return, | stats or | top)";
  }
  return null;
}

export function searchWindow(input: Pick<SplunkInputT, "earliest" | "latest">, ctx: Pick<ToolContext, "range" | "focusTime" | "beforeMs">): { earliest: string; latest: string; note: string } {
  const [r0, r1] = ctx.range.map((x) => Date.parse(x)) as [number, number];
  const f = ctx.focusTime ? Date.parse(ctx.focusTime) : NaN;
  const clip = (t: number) => new Date(Math.min(Math.max(t, r0), r1)).toISOString();
  const before = ctx.beforeMs ?? BEFORE_MS;
  const e = input.earliest ? Date.parse(input.earliest) : Number.isNaN(f) ? r0 : f - before;
  const l = input.latest ? Date.parse(input.latest) : Number.isNaN(f) ? r1 : f + AFTER_MS;
  const how = input.earliest || input.latest ? "as requested" : Number.isNaN(f) ? "the whole data range" : `default: ${Math.round(before / 3600_000)}h before to 1h after the target's time ${ctx.focusTime}`;
  return { earliest: clip(Number.isNaN(e) ? r0 : e), latest: clip(Number.isNaN(l) ? r1 : l), note: how };
}

export function splunkTool(backend: SearchBackend, index: string): EvidenceTool<SplunkInputT> {
  return {
    name: "splunk_query",
    description: `Run one read-only SPL search on index=${index}. Every search needs index=${index}, a sourcetype filter and field filters; the time window is set by the earliest/latest parameters (default: around the target's time), never in the SPL. Put plain keywords first ("svc-example" or TERM(192.0.2.10)): Splunk finds them in its index, while field=*value* filters read and parse every event. Aggregate first (stats/tstats count, values(), min/max(_time) by the identity or resource); a search without a reporting command that matches over ${PREFLIGHT_MAX} events is refused with the top values of its fields so you can narrow it. Raw events: at most 20 shown; tables: 50 rows. State the purpose and the result you expect. Splunk convention: events come back newest first, so in stats first() is the most recent and last() the earliest; earliest() and latest() go by _time.`,
    inputSchema: SplunkInput,
    readOnly: true,
    async run(input, ctx) {
      const scoped = scopeSpl(input.spl, ctx.allowedDomains, ctx.allDomains);
      if ("refused" in scoped) return { ok: false, error: `refused: ${scoped.refused}` };
      const why = validateSpl(scoped.spl, index);
      if (why) return { ok: false, error: `refused: ${why}` };
      // Mapping mode (environment scoping): aggregates only, and rows seen fewer than minCount times are cut in
      // code. Rare rows are what an incident looks like; a map of the environment must not carry them.
      let spl = scoped.spl;
      if (ctx.minCount) {
        const stats = spl.split("|").find((seg) => /^\s*(stats|tstats)\b/.test(seg));
        if (!stats || !/\bcount\b(?!\s*\()/.test(stats) || /\bcount\s+as\b/i.test(stats)) return { ok: false, error: `refused: mapping searches must aggregate with stats count ... by <fields> (a plain count column), so rare rows can be left out` };
        // A beacon is frequent by nature, so the count floor alone keeps it. Destinations are left out: the map
        // says where activity comes from, never where one host's connections go.
        const by = stats.split(/\bby\b/i)[1] ?? "";
        if (/\b(id_resp_\w*|resp_\w*|dest\w*|dst\w*|remote\w*|destination\w*)\b/i.test(by)) return { ok: false, error: `refused: mapping searches do not group by destination addresses or ports; map where activity comes from (sources, identities, hosts), not where single connections go` };
        spl = `${spl} | where count >= ${ctx.minCount}`;
      }
      const w = searchWindow(input, ctx);
      const q = { spl, earliest: w.earliest, latest: w.latest };
      const head = `purpose: ${input.purpose}\nwindow: ${w.earliest} → ${w.latest} (${w.note})${spl !== input.spl ? `\nsearch as run: ${spl}` : ""}${ctx.minCount ? `\nmapping mode: rows seen fewer than ${ctx.minCount} times are left out` : ""}`;
      if (!REPORTING.test(scoped.spl)) {
        const n = await backend.count(q, ctx.timeoutMs);
        if (n > PREFLIGHT_MAX) {
          const top = await backend.search({ ...q, spl: `${scoped.spl} | head 5000 | fieldsummary maxvals=5 | where distinct_count>1 AND distinct_count<=200 | sort - count | head 10 | table field distinct_count values` }, ctx.timeoutMs);
          return { ok: false, error: `refused: ${n.toLocaleString("en-US")} events match, over the ${PREFLIGHT_MAX} limit for raw events. Aggregate (| stats count ... by <field>) or add field filters. Top values of its fields:\n${top}\n${head}` };
        }
      }
      const text = await backend.search(q, ctx.timeoutMs);
      const wild = [...scoped.spl.split("|")[0]!.matchAll(/\b([\w.{}@-]+)\s*=\s*"?\*([^\s"*)]+)\*?"?/g)].map((m) => m[2]!);
      const hint = wild.length ? `\nNOTE: leading-wildcard field filters (${wild.slice(0, 3).map((w) => `*${w}*`).join(", ")}) make Splunk parse every event in the window. A plain keyword ("${wild[0]}") uses the index and is much faster.` : "";
      if (text.startsWith("ERROR:") || text.startsWith("refused:")) return { ok: false, error: `${text.replace(/^ERROR:\s*/, "")}\n${head}` };
      const empty = /^0 events matched, 0 results/.test(text) ? "\nNOTE: nothing matched. Against your stated expectation this is evidence, if the search names the right fields and window." : "";
      return { ok: true, data: `${head}\n${text}${empty}${hint}` };
    },
  };
}
