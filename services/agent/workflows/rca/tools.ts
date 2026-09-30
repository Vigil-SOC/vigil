// The two tools a root-cause run's investigator holds. Neither is a new integration:
// splunk_query runs on the deployment's own splunk_execute, through the same invoke
// path every run uses, and adds only what the checks and the method need -- both time
// bounds, the rows the model was shown, the rare values of a long result, and a word
// when a field the search names does not exist.
import { defineTool, type RegisteredTool } from "../../contracts/tool.js";
import type { Findings } from "./findings.js";
import { fieldsOf, type Survey } from "./survey.js";
import type { Row, Siem } from "./siem.js";

const ROWS = 50;
// Fetched so the rare values of a long result can be seen; only ROWS are shown.
const FETCH = 200;
const RAW_CHARS = 2_000;
const LIST_ITEMS = 50;

// Splunk's bookkeeping fields, never useful to the model.
const hidden = (key: string) => /^_(bkt|cd|si|serial|indextime|sourcetype|eventtype_color|kv|subsecond)$/.test(key) || key === "linecount" || key === "splunk_server";
const capped = (value: string) => (value.length > RAW_CHARS ? `${value.slice(0, RAW_CHARS)} …[+${value.length - RAW_CHARS} chars truncated]` : value);

// Every value capped, not only _raw: one values() row can hold a hundred thousand items.
function shown(row: Row): Row {
  const out: Row = {};
  for (const [key, value] of Object.entries(row)) {
    if (hidden(key)) continue;
    out[key] =
      typeof value === "string"
        ? capped(value)
        : Array.isArray(value)
          ? [
              ...value.slice(0, LIST_ITEMS).map((one) => (typeof one === "string" ? capped(one) : one)),
              ...(value.length > LIST_ITEMS ? [`…[+${value.length - LIST_ITEMS} more values; count them with stats dc() or page with mvindex]`] : []),
            ]
          : value;
  }
  return out;
}

function fieldsIn(row: Row): Row {
  if (typeof row["_raw"] !== "string") return row;
  try {
    return { ...JSON.parse(row["_raw"]), ...row };
  } catch {
    return row;
  }
}

// Nested JSON flattened to SPL-style paths (user.username, sourceIPs{}), scalars only.
function paths(value: unknown, prefix = "", out = new Map<string, string>()): Map<string, string> {
  if (Array.isArray(value)) {
    if (value.every((one) => typeof one !== "object")) out.set(`${prefix}{}`, value.join(","));
    else value.forEach((one) => paths(one, `${prefix}{}`, out));
  } else if (value !== null && typeof value === "object") {
    for (const [key, held] of Object.entries(value)) paths(held, prefix ? `${prefix}.${key}` : key, out);
  } else if (value !== null && value !== undefined && prefix) out.set(prefix, String(value));
  return out;
}

// The first rows of a result are whatever sorted first; what is rare is usually what
// matters. Values held by at most three rows, in fields with a handful of distinct
// values (who, what, from where).
export function rare(rows: readonly Row[], shownCount: number): { note: string; extra: Row[] } {
  if (rows.length < 8) return { note: "", extra: [] };
  const byField = new Map<string, Map<string, number[]>>();
  const having = new Map<string, number>();
  rows.forEach((row, index) => {
    const own = Object.fromEntries(Object.entries(fieldsIn(row)).filter(([key]) => !key.startsWith("_")));
    for (const [field, value] of paths(own)) {
      if (value.length > 150) continue;
      having.set(field, (having.get(field) ?? 0) + 1);
      const values = byField.get(field) ?? new Map<string, number[]>();
      values.set(value, [...(values.get(value) ?? []), index]);
      byField.set(field, values);
    }
  });
  const found: string[] = [];
  const beyond = new Set<number>();
  for (const [field, values] of [...byField].sort((a, b) => a[1].size - b[1].size)) {
    const n = having.get(field) ?? rows.length;
    // Constant, or an id-like column.
    if (values.size < 2 || values.size > n / 3) continue;
    for (const [value, at] of values) {
      if (at.length > 3 || at.length >= n / 4) continue;
      found.push(`${field}=${value.slice(0, 80)} (${at.length} of ${n})`);
      at.filter((index) => index >= shownCount).forEach((index) => beyond.add(index));
    }
  }
  if (found.length === 0) return { note: "", extra: [] };
  const more = found.length > 25 ? ` …+${found.length - 25}` : "";
  return {
    note: `RARE among the ${rows.length} rows: ${found.slice(0, 25).join("; ")}${more}`,
    extra: [...beyond].slice(0, 5).map((index) => shown(rows[index]!)),
  };
}

// Fields the search names that no row has. A wrong name returns blanks or zero rows,
// not an error, and a model reads that as absence.
async function missingFields(siem: Siem, found: Survey, spl: string, rows: readonly Row[], signal?: AbortSignal): Promise<string[]> {
  const sourcetype = spl.match(/\bsourcetype\s*=\s*"?([\w:.\-]+)"?/)?.[1];
  if (sourcetype === undefined) return [];
  const named = new Set<string>();
  const pipes = spl.split("|").slice(1).join("|");
  for (const match of pipes.matchAll(/\b(?:table|fields)\s+([^|]+)/g)) match[1]!.split(/[\s,]+/).filter((f) => f && !/^[-+]$/.test(f)).forEach((f) => named.add(f));
  for (const match of pipes.matchAll(/\bby\s+([^|]+)/g)) match[1]!.split(/[\s,]+/).filter(Boolean).forEach((f) => named.add(f));
  for (const match of pipes.matchAll(/\b(?:values|dc|count|min|max|latest|earliest|first|last|sum|avg)\(([^)]+)\)/g)) named.add(match[1]!);
  const base = spl.split("|")[0] ?? "";
  const filters = [...base.matchAll(/(?:^|[\s(])"?([A-Za-z_@][\w.{}@/:-]*)"?\s*!?=/g)]
    .map((match) => match[1]!)
    .filter((field) => !/^(index|sourcetype|source|host|earliest|latest)$/.test(field));
  const wanted = [...named]
    .map((field) => field.replace(/^"|"$/g, ""))
    .filter((field) => field && !field.startsWith("_") && !/^(sort|head|limit|count|as)$/i.test(field) && !/[=()]/.test(field));
  const blank = (row: Row, field: string) => {
    const own = fieldsIn(row);
    const value = own[field] ?? paths(own).get(field);
    return value === undefined || value === null || value === "";
  };
  const checks = rows.length > 0 ? wanted.filter((field) => rows.every((row) => blank(row, field))) : filters;
  if (checks.length === 0) return [];
  const surveyed = found.sources.find((source) => source.sourcetype === sourcetype);
  const all = surveyed?.fields.length ? surveyed.fields : await fieldsOf(siem, surveyed?.index ?? "", sourcetype, signal);
  if (all.length === 0) return [];
  const leaf = (name: string) => name.toLowerCase().split(/[.{}]+/).filter(Boolean).pop() ?? name.toLowerCase();
  return [...new Set(checks)]
    .filter((field) => !all.includes(field))
    .map((field) => {
      const near = all.filter((real) => real.toLowerCase().includes(leaf(field)) || leaf(field).includes(leaf(real))).slice(0, 6);
      return `FIELD: "${field}" is not a field of ${sourcetype}${near.length ? `; similar real fields: ${near.join(", ")}` : ""}`;
    });
}

export interface Shown {
  spl: string;
  earliest?: string;
  latest?: string;
  // One compact JSON line per row the model was shown, as findings read them back.
  lines: string[];
}

export interface ToolContext {
  siem: Siem;
  survey: Survey;
  findings: Findings;
  // What was shown, journaled so a resume knows what the model has seen.
  record(shown: Shown): Promise<void>;
  // Whatever the run wants riding along with every result: open whys, a nudge.
  footer(): string;
}

const secondsOf = (iso: unknown): number | undefined => {
  if (typeof iso !== "string" || iso.trim() === "") return undefined;
  const at = Date.parse(iso);
  return Number.isNaN(at) ? Number.NaN : at / 1000;
};

// Result rows plus notes, each a row: the harness renders rows and nothing else.
export function splunkQueryTool(context: ToolContext, index: string): RegisteredTool {
  return defineTool(
    {
      id: "splunk_query",
      description:
        `Run one SPL search. earliest and latest (ISO 8601) are optional; the default is everything the index holds. Returns up to ${ROWS} rows ` +
        "(events or the table a stats/table/top produces), rare values among a long result, and a warning when a field you named is not in the " +
        "sourcetype. Events come back newest first, so in stats first() is the most recent event and last() the earliest; earliest() and " +
        "latest() go by _time.",
      parameters: {
        type: "object",
        required: ["spl"],
        properties: {
          spl: { type: "string", description: `SPL, e.g. index=${index} sourcetype=<sourcetype> <field>=<value>` },
          earliest: { type: "string", description: "ISO 8601" },
          latest: { type: "string", description: "ISO 8601" },
        },
      },
      execute: async (args, _bounds, signal) => {
        const spl = typeof args["spl"] === "string" ? args["spl"] : "";
        if (spl.trim() === "") return { ok: false, failure: { kind: "invalid_args", detail: "spl is required" } };
        const earliest = secondsOf(args["earliest"]);
        const latest = secondsOf(args["latest"]);
        if (Number.isNaN(earliest) || Number.isNaN(latest)) return { ok: false, failure: { kind: "invalid_args", detail: "earliest and latest must be ISO 8601 times" } };

        const searched = await context.siem.search(
          spl,
          { ...(earliest === undefined ? {} : { earliest }), ...(latest === undefined ? {} : { latest }) },
          FETCH,
          signal,
        );
        if (searched.error !== undefined) return { ok: false, failure: { kind: "backend_error", detail: searched.error } };

        const rows = searched.rows.slice(0, ROWS).map(shown);
        const odd = rare(searched.rows, rows.length);
        const fields = await missingFields(context.siem, context.survey, spl, searched.rows, signal).catch(() => []);
        const lines = [...rows, ...odd.extra].map((row) => JSON.stringify(row));
        context.findings.saw(lines);
        await context.record({ spl, ...(typeof args["earliest"] === "string" ? { earliest: args["earliest"] } : {}), ...(typeof args["latest"] === "string" ? { latest: args["latest"] } : {}), lines });

        const notes = [
          searched.rows.length > rows.length ? `showing ${rows.length} of ${searched.rows.length}${searched.rows.length >= FETCH ? "+" : ""} rows: aggregate or narrow to see the rest` : "",
          ...fields,
          odd.note,
          odd.extra.length ? `rare rows beyond the ${rows.length} shown follow` : "",
          context.footer(),
        ].filter(Boolean);
        const out = [...rows, ...notes.map((note) => ({ note })), ...odd.extra.map((row) => ({ rare_row: row }))];
        return { ok: true, rows: out, rowCount: out.length, capped: searched.rows.length > rows.length, sourceSystem: context.siem.tool };
      },
    },
    // Up to three searches a call (the search, a field sample, and the store's own
    // retries), each on the telemetry bound.
    { maxRows: ROWS + 40, timeoutMs: 400_000 },
    true,
  );
}

export function findingsTool(context: ToolContext, save: () => Promise<void>): RegisteredTool {
  return defineTool(
    {
      id: "findings",
      description:
        "Your findings file, your memory of the investigation. op=read returns every finding and hypothesis. op=write records a finding, or " +
        "updates one when you pass its id (e.g. once you find its why); every finding must answer when, who, what and why, from the events. " +
        "op=hypothesis records what you think happened before you can prove it (text), and the search or event that would confirm or refute " +
        "it (test); pass its id with status confirmed/refuted to update it. Hypotheses need no proof and are never part of the proven chain.",
      parameters: {
        type: "object",
        required: ["op"],
        properties: {
          op: { type: "string", enum: ["read", "write", "hypothesis"] },
          text: { type: "string", description: "hypothesis: what you think happened" },
          test: { type: "string", description: "hypothesis: the search or event that would confirm or refute it" },
          status: { type: "string", enum: ["open", "confirmed", "refuted"], description: "hypothesis: its state" },
          id: { type: "string", description: "write: F1, F2, … to update one; omit for a new finding" },
          what: { type: "string", description: "the action: what was done, to what" },
          who: { type: "string", description: "the identity that performed it, copied exactly from the event (user, key, role, process, host)" },
          session: { type: "string", description: "the credential or session behind it, copied exactly from the event (access key id, session/request id, pod, process id), or 'none' if the event has none" },
          when: { type: "string", description: "event time" },
          evidence: { type: "string", description: "a distinctive value copied exactly from the event row (event id, command line, request id)" },
          link: {
            type: "string",
            description:
              "when why names a finding: the value that connects the two, something the cause's event produced and this event used (commit sha, " +
              "image digest, function/role/object name, key id, pod, request id), copied exactly. It must appear in a row with the cause's evidence " +
              "and in a row with this finding's evidence. Never an IP, a time or an identity (role, user, session, service account, email). Or " +
              "'correlated' when a process's network event (DNS or connection to a cloud service) and that service's API call happen within 3 " +
              "seconds; this is weaker than a shared value and is labelled correlated.",
          },
          why: {
            type: "string",
            description:
              "the id of the finding that caused or enabled it (e.g. \"F2: its token was used here\"); the id may be one you have not recorded yet, " +
              "and stays open until you do. 'origin' if nothing in the logs comes before it. Or 'unknown'. A label (\"unusual IP\", \"defense evasion\") is not a why.",
          },
        },
      },
      execute: async (args) => {
        const answer = await context.findings.call(args);
        if (args["op"] !== "read") await save();
        return { ok: true, rows: [{ findings: answer }], rowCount: 1, capped: false, sourceSystem: "findings" };
      },
    },
    // A write asks the store about a link's provenance: a few searches at most.
    { maxRows: 1, timeoutMs: 600_000 },
    true,
  );
}
