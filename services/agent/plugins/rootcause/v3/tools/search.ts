// What the one evidence tool runs: an SPL search, rendered as text for the model (ported from v1's
// splunk_query: match counts, Splunk errors/warnings verbatim, capped rows, rare values, unknown-field hints).
import type { JobRunner, Row } from "./job.js";

export interface QueryInput { spl: string; earliest?: string; latest?: string }

export interface SearchBackend {
  search(input: QueryInput, timeoutMs?: number): Promise<string>;
  count(input: QueryInput, timeoutMs?: number): Promise<number>; // events the search matches; -1 if unknown
}

// A search that reduces events to a table: its output is small whatever the match count.
export const REPORTING = /(^\s*\|\s*tstats\b)|\|\s*(stats|tstats|chart|timechart|top|rare|fieldsummary|eventcount)\b/i;

const ROWS = 50; // table rows shown
const RAW_ROWS = 20; // raw events shown
const FETCH = 200; // fetched, for rare values; only ROWS are shown
const RAW_CHARS = 2000;
const RESULT_CHARS = 20_000;
const LIST_ITEMS = 50;
export const WRITES = /\|\s*(delete|collect|outputlookup|outputcsv|sendemail|tscollect|mcollect|meventcollect|script|run|sendalert)\b/i;

const hidden = (k: string) => /^_(bkt|cd|si|serial|indextime|sourcetype|eventtype_color|kv|subsecond)$/.test(k) || k === "linecount" || k === "splunk_server";
const capStr = (v: string) => (v.length > RAW_CHARS ? v.slice(0, RAW_CHARS) + ` …[+${v.length - RAW_CHARS} chars truncated]` : v);
function show(row: Row): string {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(row)) {
    if (hidden(k)) continue;
    out[k] = typeof v === "string" ? capStr(v)
      : Array.isArray(v) ? [...v.slice(0, LIST_ITEMS).map((x) => (typeof x === "string" ? capStr(x) : x)), ...(v.length > LIST_ITEMS ? [`…[+${v.length - LIST_ITEMS} more values; count them with stats dc() or page with mvindex]`] : [])]
      : v;
  }
  return JSON.stringify(out);
}

function fieldsOf(row: Row): Row {
  if (typeof row._raw !== "string") return row;
  try { return { ...JSON.parse(row._raw), ...row }; } catch { return row; }
}

function flatten(o: unknown, prefix = "", out = new Map<string, string>()): Map<string, string> {
  if (Array.isArray(o)) { if (o.every((v) => typeof v !== "object")) out.set(`${prefix}{}`, o.join(",")); else o.forEach((v) => flatten(v, `${prefix}{}`, out)); }
  else if (o && typeof o === "object") for (const [k, v] of Object.entries(o)) flatten(v, prefix ? `${prefix}.${k}` : k, out);
  else if (o !== null && o !== undefined && prefix) out.set(prefix, String(o));
  return out;
}

// The first rows are whatever sorted first; what is rare is usually what matters.
function rare(rows: Row[], shown: number): string {
  if (rows.length < 8) return "";
  const byField = new Map<string, Map<string, number[]>>();
  const seen = new Map<string, number>();
  rows.forEach((row, i) => {
    const f = fieldsOf(row);
    for (const [k, v] of flatten(Object.fromEntries(Object.entries(f).filter(([k]) => !k.startsWith("_"))))) {
      if (v.length > 150) continue;
      seen.set(k, (seen.get(k) ?? 0) + 1);
      const m = byField.get(k) ?? new Map<string, number[]>();
      (m.get(v) ?? m.set(v, []).get(v)!).push(i);
      byField.set(k, m);
    }
  });
  const out: string[] = [];
  const extra = new Set<number>();
  for (const [field, vals] of [...byField].sort((a, b) => a[1].size - b[1].size)) {
    const n = seen.get(field) ?? rows.length;
    if (vals.size < 2 || vals.size > n / 3) continue;
    for (const [val, idx] of vals) if (idx.length <= 3 && idx.length < n / 4) {
      out.push(`${field}=${val.slice(0, 80)} (${idx.length} of ${n})`);
      idx.filter((i) => i >= shown).forEach((i) => extra.add(i));
    }
  }
  if (!out.length) return "";
  const more = [...extra].slice(0, 5).map((i) => show(rows[i]!));
  return `RARE among the ${rows.length} rows: ${out.slice(0, 25).join("; ")}${out.length > 25 ? ` …+${out.length - 25}` : ""}` +
    (more.length ? `\nrare rows beyond the ${shown} shown:\n${more.join("\n")}` : "");
}

const quote = (v: string) => `"${v.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;

export class LiveSearch implements SearchBackend {
  private fieldCache = new Map<string, Promise<string[]>>();
  constructor(private client: JobRunner, private index: string, private range: [string, string]) {}

  fieldsIn(sourcetype: string): Promise<string[]> {
    if (!this.fieldCache.has(sourcetype)) this.fieldCache.set(sourcetype, this.client.runJob(`index=${this.index} sourcetype=${quote(sourcetype)} | head 2000 | fieldsummary | where count > 0 | fields field`, { earliest: 0, latest: 2e9, limit: 500 })
      .then((j) => j.rows.map((r) => String(r.field)).filter((f) => !f.startsWith("_") && !/^date_|^punct$|^linecount$|^splunk_server|^timestartpos|^timeendpos/.test(f))).catch(() => []));
    return this.fieldCache.get(sourcetype)!;
  }

  // Fields the search names that no row has: a wrong name returns blanks or zero rows, not an error.
  private async missingFields(spl: string, rows: Row[], events: number): Promise<string[]> {
    const st = spl.match(/\bsourcetype\s*=\s*"?([\w:.\-]+)"?/)?.[1];
    if (!st) return [];
    const named = new Set<string>();
    const pipes = spl.split("|").slice(1).join("|");
    for (const m of pipes.matchAll(/\b(?:table|fields)\s+([^|]+)/g)) m[1]!.split(/[\s,]+/).filter((f) => f && !/^[-+]$/.test(f)).forEach((f) => named.add(f));
    for (const m of pipes.matchAll(/\bby\s+([^|]+)/g)) m[1]!.split(/[\s,]+/).filter(Boolean).forEach((f) => named.add(f));
    for (const m of pipes.matchAll(/\b(?:values|dc|count|min|max|latest|earliest|first|last|sum|avg)\(([^)]+)\)/g)) named.add(m[1]!);
    const base = spl.split("|")[0]!;
    const filters = [...base.matchAll(/(?:^|[\s(])"?([A-Za-z_@][\w.{}@/:-]*)"?\s*!?=/g)].map((m) => m[1]!).filter((f) => !/^(index|sourcetype|source|host|earliest|latest)$/.test(f));
    const want = [...named].map((f) => f.replace(/^"|"$/g, "")).filter((f) => f && !f.startsWith("_") && !/^(sort|head|limit|count|as)$/i.test(f) && !/[=()]/.test(f));
    const checks = rows.length ? want.filter((f) => rows.every((r) => { const v = fieldsOf(r)[f] ?? flatten(fieldsOf(r)).get(f); return v === undefined || v === null || v === ""; }))
      : events === 0 ? filters : [];
    if (!checks.length) return [];
    const all = await this.fieldsIn(st);
    if (!all.length) return [];
    const near = (name: string) => {
      const last = name.toLowerCase().split(/[.{}]+/).filter(Boolean).pop() ?? name.toLowerCase();
      return all.filter((f) => f.toLowerCase().includes(last) || last.includes(f.toLowerCase().split(/[.{}]+/).filter(Boolean).pop() ?? "~")).slice(0, 6);
    };
    return [...new Set(checks)].filter((f) => !all.includes(f)).map((f) => {
      const n = near(f);
      return `FIELD: "${f}" is not a field of ${st}${n.length ? `; similar real fields: ${n.join(", ")}` : ""}`;
    });
  }

  async count({ spl, earliest, latest }: QueryInput, timeoutMs?: number): Promise<number> {
    const e = Date.parse(earliest ?? this.range[0]), l = Date.parse(latest ?? this.range[1]);
    try {
      const job = await this.client.runJob(`${spl} | stats count`, { earliest: e / 1000, latest: l / 1000, limit: 1, timeoutMs });
      return job.errors.length ? -1 : Number(job.rows[0]?.count ?? 0);
    } catch {
      return -1;
    }
  }

  // For the first stats/chart/top ... by <fields> in a search: how many of the events reaching it lack each
  // by-field. Splunk drops those events from the table without a word (a network event has no process id).
  private async droppedByFields(spl: string, earliest: number, latest: number, timeoutMs?: number): Promise<{ field: string; missing: number; total: number }[] | null> {
    const pipes = spl.split("|");
    const i = pipes.findIndex((p, k) => k > 0 && /^\s*(stats|chart|timechart|top|rare)\b/i.test(p) && /\bby\b/i.test(p));
    if (i < 0) return null;
    const fields = pipes[i]!.split(/\bby\b/i).pop()!.split(/[\s,]+/).map((f) => f.replace(/^"|"$/g, "")).filter((f) => f && !/[=()]/.test(f) && !/^(limit|useother|usenull)$/i.test(f)).slice(0, 6);
    if (!fields.length) return null;
    const prefix = pipes.slice(0, i).join("|");
    const job = await this.client.runJob(`${prefix} | stats count as total ${fields.map((f, k) => `count("${f}") as f${k}`).join(" ")}`, { earliest, latest, limit: 1, timeoutMs });
    const row = job.rows[0];
    if (!row || job.errors.length) return null;
    const total = Number(row.total ?? 0);
    return fields.map((field, k) => ({ field, missing: total - Number(row[`f${k}`] ?? 0), total })).filter((d) => d.missing > 0 && d.missing >= d.total * 0.1);
  }

  async search({ spl, earliest, latest }: QueryInput, timeoutMs?: number): Promise<string> {
    if (WRITES.test(spl)) return "refused: read-only, write commands are blocked";
    const e = Date.parse(earliest ?? this.range[0]), l = Date.parse(latest ?? this.range[1]);
    if (Number.isNaN(e) || Number.isNaN(l)) return "refused: earliest and latest must be ISO 8601 times";
    try {
      const job = await this.client.runJob(spl, { earliest: e / 1000, latest: l / 1000, limit: FETCH, timeoutMs });
      const shown = job.rows.slice(0, REPORTING.test(spl) ? ROWS : RAW_ROWS);
      const lines = [`${job.events.toLocaleString("en-US")} events matched, ${job.results.toLocaleString("en-US")} results (showing ${shown.length} of ${job.results.toLocaleString("en-US")})${job.finalized ? " — timed out, counts partial" : ""}`];
      for (const m of job.errors) lines.push(`ERROR: ${m}`);
      for (const m of job.warnings) lines.push(`WARNING: ${m}`);
      const slow = job.finalized || job.seconds > 20; // no extra passes over a source that is already slow
      if (job.events > 0 && !job.errors.length && !slow) {
        const drop = await this.droppedByFields(spl, e / 1000, l / 1000, timeoutMs).catch(() => null);
        if (drop?.length) lines.push(`NOTE: stats leaves out events where a by-field is missing. ${drop.map((d) => `"${d.field}" is missing on ${d.missing.toLocaleString("en-US")} of ${d.total.toLocaleString("en-US")} matched events`).join("; ")}. Those events are not in the table. Use fillnull value=- ${drop.map((d) => d.field).join(" ")}, group by fewer fields, or look at them without stats.`);
        else if (!drop && job.results === 0 && /\|\s*(stats|chart|timechart|top|rare)\b.*\bby\b/i.test(spl))
          lines.push(`NOTE: ${job.events.toLocaleString("en-US")} events matched but the table is empty: stats drops events where any by-field is null. List the by-fields in fillnull (| fillnull value=- <field> <field> ...) so missing ones are created, or use fewer by-fields.`);
      }
      for (const r of shown) lines.push(show(r));
      if (!slow) lines.push(...(await this.missingFields(spl, job.rows, job.events).catch(() => [])));
      if (job.finalized) lines.push("NOTE: the search hit the time limit and returned what it had: counts are partial. Narrow it with plain keywords or a shorter window.");
      const r = rare(job.rows, shown.length);
      if (r) lines.push(r);
      const text = lines.join("\n");
      return text.length > RESULT_CHARS ? text.slice(0, RESULT_CHARS) + `\n…[result cut at ${RESULT_CHARS} of ${text.length} chars: aggregate or narrow the search]` : text;
    } catch (err: any) {
      return `ERROR: ${err.message}`;
    }
  }
}
