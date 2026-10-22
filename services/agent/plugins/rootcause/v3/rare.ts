// Rare-activity sweep, run by code once per run: for each source, the least frequent values of
// its descriptive fields (process, command, file, action, actor, verb, event name) over the whole data range.
// The model searches for the records its stories imagine; when the guessed mechanism is a little off, a keyword
// filter misses the real steps even on the right host and range (an attacker's
// tooling once never came up). The environment map keeps only what is common; this keeps what is rare. Rare is not malicious: it is
// where an analyst looks first. The result goes into the cached system context, with citable search ids.
import pLimit from "p-limit";
import type { Domain } from "./types.js";
import type { ToolRegistry } from "./tools/registry.js";
import type { TurnTrace } from "./trace.js";

// Field names common across log products (EDR, Sysmon, CloudTrail, Kubernetes audit, GitHub audit, Windows).
const CANDIDATES = ["ImageFileName", "Image", "ParentBaseFileName", "ParentImage", "process_name", "CommandLine", "TargetFileName", "FileName", "event_simpleName", "EventCode",
  "eventName", "eventSource", "userAgent", "verb", "objectRef.resource", "objectRef.subresource", "user.username", "action", "actor", "query", "service"];
const HOSTS = ["ComputerName", "Computer", "hostname", "host"];
const MAX_FIELDS = 4; // per source
const MIN_DC = 3, MAX_DC = 3000; // fewer is not a distribution; more is mostly unique ids
const TOP = 8; // rarest values listed per field

// signal: a stop or a lost lease; no search starts after it (the caller throws it once the sweep returns).
export interface RareDeps { tools: ToolRegistry; trace: TurnTrace; index: string; range: [string, string]; domains: Domain[]; timeoutMs: number; signal?: AbortSignal }

const CONCURRENCY = 4; // searches in flight across the whole sweep

const q = (f: string) => (/^[\w]+$/.test(f) ? f : `'${f}'`); // eval/where need quotes around dotted names
const rows = (data?: string) => (data ?? "").split("\n").filter((l) => l.startsWith("{")).map((l) => { try { return JSON.parse(l) as Record<string, string>; } catch { return null; } }).filter((x): x is Record<string, string> => !!x);
const iso = (t: string) => new Date(Number(t) * 1000).toISOString().slice(0, 16).replace("T", " ");

type Searched = { id: string; ok: boolean; rows: Record<string, string>[] };

// Runs the sweep's searches up to CONCURRENCY at a time. They start (and so take their refs) in the order they
// are asked for, and are traced in that order too, whatever order they finish in.
function searcher(d: RareDeps) {
  const limit = pLimit(CONCURRENCY);
  const started: Promise<unknown>[] = [];
  let traced: Promise<unknown> = Promise.resolve();
  const search = (purpose: string, spl: string): Promise<Searched> => {
    const input = { purpose, spl, earliest: d.range[0], latest: d.range[1] };
    const rec = limit(() => {
      d.signal?.throwIfAborted(); // queued behind a stop: never started, never numbered
      const call = d.tools.call("splunk_query", input, { allowedDomains: d.domains, allDomains: d.domains, timeoutMs: d.timeoutMs, range: d.range });
      started.push(call.catch(() => {}));
      return call;
    });
    rec.catch(() => {}); // stopped while queued: handled below, once the searches before it are traced
    const out = traced.then(() => rec).then((rec) => {
      d.trace.write("query", { toolCallId: rec.toolCallId, tool: "splunk_query", input, rare: true });
      d.trace.write("tool-output", { toolCallId: rec.toolCallId, ok: rec.result.ok, output: rec.result.ok ? rec.result.data : rec.result.error });
      return { id: rec.toolCallId, ok: rec.result.ok, rows: rows(rec.result.data) };
    });
    traced = out.catch(() => {});
    out.catch(() => {}); // awaited later, in order; not an unhandled rejection meanwhile
    return out;
  };
  // Every search that started has finished (and is journaled), so nothing is written after the sweep returns.
  const settle = async () => { while (started.length) await Promise.allSettled(started.splice(0)); };
  return { search, settle };
}

export async function rareSweep(d: RareDeps): Promise<string> {
  const { search, settle } = searcher(d);
  // Per source, its fields search before its per-field searches; a source's per-field searches overlap the next
  // source's. The text is assembled in source and field order once they are all back, as if run one by one.
  const sources: { st: string; failed?: string; r?: Record<string, string>; fields: { f: string; s: Promise<Searched> }[] }[] = [];
  try {
    for (const st of d.domains) {
      const base = `index=${d.index} sourcetype="${st}"`;
      // Which candidate fields this source has, and how many distinct values each takes.
      const dc = await search(`rare sweep: which descriptive fields ${st} has`, `${base} | stats ${[...CANDIDATES, ...HOSTS].map((f) => `dc(${q(f)}) as ${q(f)}`).join(" ")}`);
      const r = dc.rows[0];
      if (!dc.ok || !r) { sources.push({ st, failed: dc.id, fields: [] }); continue; }
      const fields = CANDIDATES.filter((f) => Number(r[f] ?? 0) >= MIN_DC && Number(r[f]) <= MAX_DC).slice(0, MAX_FIELDS);
      const host = HOSTS.find((h) => Number(r[h] ?? 0) >= 1);
      sources.push({ st, r, fields: fields.map((f) => {
        const spl = `${base} ${q(f)}=* | stats count min(_time) as first max(_time) as last${host ? ` dc(${q(host)}) as hosts values(${q(host)}) as h` : ""} by ${q(f)} | sort 0 count first | head ${TOP}${host ? ` | eval h=mvjoin(mvindex(h,0,1),",")` : ""}`;
        return { f, s: search(`rare sweep: least frequent ${f} in ${st}`, spl) };
      }) });
    }
    const out: string[] = [];
    for (const { st, failed, r, fields } of sources) {
      if (failed) { out.push(`- ${st}: sweep failed [${failed}]`); continue; }
      const lines: string[] = [];
      for (const { f, s: p } of fields) {
        const s = await p;
        if (!s.ok || !s.rows.length) continue;
        const vals = s.rows.map((x) => `${String(x[f]).slice(0, 100)} ×${x.count} (${iso(x.first!)}${x.count !== "1" ? ` → ${iso(x.last!)}` : ""}${x.h ? `, ${x.h}${Number(x.hosts) > 2 ? ` +${Number(x.hosts) - 2}` : ""}` : ""})`);
        lines.push(`  ${f} (${r![f]} distinct) [${s.id}]: ${vals.join("; ")}`);
      }
      if (lines.length) out.push(`- ${st}:`, ...lines);
    }
    if (!out.length) return "";
    return `## Rare activity (swept by code over the whole data range)\n\nThe least frequent values of each source's descriptive fields, with count, first → last time and hosts. Rare is not malicious, and nothing here is judged; but an attack's own steps are often among the rarest records, so check whether any of these belong to the incident before searching for records you only imagine. Cite the search ids.\n\n${out.join("\n")}`;
  } finally {
    await settle();
  }
}

// The values the sweep listed, with their source and field, rarest first per field. Values seen at most a few
// times only: those are the outliers worth a test.
export interface RareValue { value: string; sourcetype: string; field: string; count: number }
export function rareValues(text: string | undefined, maxCount = 5): RareValue[] {
  const out: RareValue[] = [];
  let st = "";
  for (const line of (text ?? "").split("\n")) {
    const s = line.match(/^- (\S+):\s*$/);
    if (s) { st = s[1]!; continue; }
    const m = line.match(/^\s+(\S+) \(\d+ distinct\) \[\w+\]: (.*)$/);
    if (!m || !st) continue;
    for (const part of m[2]!.split(/; (?=\S[^;]*? ×\d+ \()/)) {
      const v = part.match(/^(.*?) ×(\d+) \(/);
      if (v && Number(v[2]) <= maxCount && v[1]!.trim().length >= 2 && !out.some((x) => x.value === v[1]!.trim())) out.push({ value: v[1]!.trim(), sourcetype: st, field: m[1]!, count: Number(v[2]) });
    }
  }
  return out;
}

// Does a text name one of the rare values? Short values (xz) must match as a whole word; paths also by basename.
export function mentionsRare(text: string, values: string[]): boolean {
  if (!values.length) return false;
  const t = text.toLowerCase();
  const esc = (x: string) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return values.some((v) => [v, v.split("/").at(-1) ?? ""].filter((x) => x.length >= 2).some((x) => {
    const y = x.toLowerCase();
    return y.length >= 6 ? t.includes(y) : new RegExp(`(^|[^a-z0-9_])${esc(y)}($|[^a-z0-9_])`).test(t);
  }));
}
