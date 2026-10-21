// Rare-activity sweep, run by code once per run: for each source, the least frequent values of
// its descriptive fields (process, command, file, action, actor, verb, event name) over the whole data range.
// The model searches for the records its stories imagine; when the guessed mechanism is a little off, a keyword
// filter misses the real steps even on the right host and range (an attacker's
// tooling once never came up). The environment map keeps only what is common; this keeps what is rare. Rare is not malicious: it is
// where an analyst looks first. The result goes into the cached system context, with citable search ids.
import pLimit from "p-limit";
import type { Domain } from "./types.js";
import type { ToolCallRecord, ToolRegistry } from "./tools/registry.js";
import type { TurnTrace } from "./trace.js";

// Field names common across log products (EDR, Sysmon, CloudTrail, Kubernetes audit, GitHub audit, Windows).
const CANDIDATES = ["ImageFileName", "Image", "ParentBaseFileName", "ParentImage", "process_name", "CommandLine", "TargetFileName", "FileName", "event_simpleName", "EventCode",
  "eventName", "eventSource", "userAgent", "verb", "objectRef.resource", "objectRef.subresource", "user.username", "action", "actor", "query", "service"];
const HOSTS = ["ComputerName", "Computer", "hostname", "host"];
const MAX_FIELDS = 4; // per source
const MIN_DC = 3, MAX_DC = 3000; // fewer is not a distribution; more is mostly unique ids
const TOP = 8; // rarest values listed per field

export interface RareDeps { tools: ToolRegistry; trace: TurnTrace; index: string; range: [string, string]; domains: Domain[]; timeoutMs: number; signal?: AbortSignal }

const CAP = 4; // searches in flight at once, across the whole sweep (Splunk refuses more by quota)

const q = (f: string) => (/^[\w]+$/.test(f) ? f : `'${f}'`); // eval/where need quotes around dotted names
const rows = (data?: string) => (data ?? "").split("\n").filter((l) => l.startsWith("{")).map((l) => { try { return JSON.parse(l) as Record<string, string>; } catch { return null; } }).filter((x): x is Record<string, string> => !!x);
const iso = (t: string) => new Date(Number(t) * 1000).toISOString().slice(0, 16).replace("T", " ");

interface Found { id: string; ok: boolean; rows: Record<string, string>[] }

// Searches run FIFO under one cap, so they start (and take their refs) in the order they are queued: the order a
// sequential sweep would issue them. A failed search, or one the cap or a stop kept from starting, is a dropped one.
function searcher(d: RareDeps) {
  const limit = pLimit(CAP);
  const traced: { input?: unknown; rec?: ToolCallRecord }[] = []; // in queue order, written to the trace in that order
  let error: unknown;
  const search = (purpose: string, spl: string): Promise<Found> => {
    const input = { purpose, spl, earliest: d.range[0], latest: d.range[1] };
    const slot: (typeof traced)[number] = { input };
    traced.push(slot);
    return limit(async () => {
      try {
        if (error !== undefined) throw error;
        d.signal?.throwIfAborted();
        const rec = await d.tools.call("splunk_query", input, { allowedDomains: d.domains, allDomains: d.domains, timeoutMs: d.timeoutMs, range: d.range });
        slot.rec = rec;
        return { id: rec.toolCallId, ok: rec.result.ok, rows: rows(rec.result.data) };
      } catch (e) {
        error ??= e;
        return { id: "", ok: false, rows: [] };
      }
    });
  };
  const finish = () => {
    for (const { input, rec } of traced) {
      if (!rec) continue;
      d.trace.write("query", { toolCallId: rec.toolCallId, tool: "splunk_query", input, rare: true });
      d.trace.write("tool-output", { toolCallId: rec.toolCallId, ok: rec.result.ok, output: rec.result.ok ? rec.result.data : rec.result.error });
    }
    if (error !== undefined) throw error;
  };
  return { search, finish, failed: () => error !== undefined };
}

export async function rareSweep(d: RareDeps): Promise<string> {
  const { search, finish, failed } = searcher(d);
  const sources: Promise<string[]>[] = []; // each source's lines, joined in source order
  for (const st of d.domains) {
    if (failed()) break;
    const base = `index=${d.index} sourcetype="${st}"`;
    // Which candidate fields this source has, and how many distinct values each takes.
    const dc = await search(`rare sweep: which descriptive fields ${st} has`, `${base} | stats ${[...CANDIDATES, ...HOSTS].map((f) => `dc(${q(f)}) as ${q(f)}`).join(" ")}`);
    const r = dc.rows[0];
    if (!dc.ok || !r) { sources.push(Promise.resolve([`- ${st}: sweep failed [${dc.id}]`])); continue; }
    const fields = CANDIDATES.filter((f) => Number(r[f] ?? 0) >= MIN_DC && Number(r[f]) <= MAX_DC).slice(0, MAX_FIELDS);
    const host = HOSTS.find((h) => Number(r[h] ?? 0) >= 1);
    if (!fields.length) continue;
    // Queued now, so they hold their refs before the next source's dc search; that one runs while these do.
    const found = fields.map((f) => {
      const spl = `${base} ${q(f)}=* | stats count min(_time) as first max(_time) as last${host ? ` dc(${q(host)}) as hosts values(${q(host)}) as h` : ""} by ${q(f)} | sort 0 count first | head ${TOP}${host ? ` | eval h=mvjoin(mvindex(h,0,1),",")` : ""}`;
      return search(`rare sweep: least frequent ${f} in ${st}`, spl);
    });
    sources.push(Promise.all(found).then((res) => {
      const lines: string[] = [];
      fields.forEach((f, i) => {
        const s = res[i]!;
        if (!s.ok || !s.rows.length) return;
        const vals = s.rows.map((x) => `${String(x[f]).slice(0, 100)} ×${x.count} (${iso(x.first!)}${x.count !== "1" ? ` → ${iso(x.last!)}` : ""}${x.h ? `, ${x.h}${Number(x.hosts) > 2 ? ` +${Number(x.hosts) - 2}` : ""}` : ""})`);
        lines.push(`  ${f} (${r[f]} distinct) [${s.id}]: ${vals.join("; ")}`);
      });
      return lines.length ? [`- ${st}:`, ...lines] : [];
    }));
  }
  const out = (await Promise.all(sources)).flat();
  finish();
  if (!out.length) return "";
  return `## Rare activity (swept by code over the whole data range)\n\nThe least frequent values of each source's descriptive fields, with count, first → last time and hosts. Rare is not malicious, and nothing here is judged; but an attack's own steps are often among the rarest records, so check whether any of these belong to the incident before searching for records you only imagine. Cite the search ids.\n\n${out.join("\n")}`;
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
