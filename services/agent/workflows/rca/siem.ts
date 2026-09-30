import type { RegisteredTool } from "../../contracts/tool.js";
import type { ToolDispatch } from "../../core/seams.js";

export type Row = Record<string, unknown>;

// Epoch seconds. Absent means the whole of what the store holds.
export interface Window {
  earliest?: number;
  latest?: number;
}

export interface Searched {
  rows: Row[];
  // The store's own words when the search did not run, never an empty result.
  error?: string;
}

// What a root-cause run asks of the store that holds the logs: one search language,
// absolute bounds, and rows back. The code checks are written over this and nothing
// wider, so a store that cannot answer it cannot run them.
export interface Siem {
  readonly tool: string;
  search(spl: string, window: Window, limit: number, signal?: AbortSignal): Promise<Searched>;
}

// The one tool a run's checks can be expressed through today: raw SPL in, rows out.
// Matched by suffix because the server's own name is a deployment's to choose.
export const SPLUNK_TOOL_SUFFIX = "splunk_execute";

export function isSplunk(toolId: string): boolean {
  return toolId === SPLUNK_TOOL_SUFFIX || toolId.endsWith(`_${SPLUNK_TOOL_SUFFIX}`);
}

// Commands that write, send or run something. A trace reads; nothing it does may
// change the store it is reading.
const WRITES = /\|\s*(delete|collect|outputlookup|outputcsv|sendemail|tscollect|mcollect|meventcollect|script|run)\b/i;

export function writes(spl: string): boolean {
  return WRITES.test(spl);
}

// splunk_execute takes an earliest and no latest, and an ISO time written into the
// search string silently matches nothing. Epoch seconds written as time modifiers
// on the base search are honoured and override the job's own, so both bounds go
// there. A generating search (| tstats, | makeresults) carries its own and is left
// as written.
export function bounded(spl: string, window: Window): string {
  const text = spl.trim();
  if (text.startsWith("|")) return text;
  const modifiers = [
    window.earliest === undefined ? "" : `earliest=${Math.floor(window.earliest)}`,
    window.latest === undefined ? "" : `latest=${Math.floor(window.latest)}`,
  ].filter(Boolean);
  if (modifiers.length === 0) return text;
  const cut = firstPipe(text);
  const base = cut < 0 ? text : text.slice(0, cut).trimEnd();
  const rest = cut < 0 ? "" : ` ${text.slice(cut)}`;
  return `${base} ${modifiers.join(" ")}${rest}`;
}

// The base search ends at the first pipe outside a quoted string.
function firstPipe(spl: string): number {
  let quoted = false;
  for (let at = 0; at < spl.length; at += 1) {
    const ch = spl[at];
    if (ch === "\\") at += 1;
    else if (ch === '"') quoted = !quoted;
    else if (ch === "|" && !quoted) return at;
  }
  return -1;
}

// splunk_execute answers with an envelope, {success, results} or {error}; the
// invoke endpoint may or may not have unwrapped it already.
function rowsOf(rows: readonly unknown[]): Searched {
  const [first] = rows;
  if (rows.length === 1 && typeof first === "object" && first !== null) {
    const envelope = first as Row;
    if (typeof envelope["error"] === "string") return { rows: [], error: envelope["error"] };
    if (Array.isArray(envelope["results"])) return { rows: envelope["results"] as Row[] };
  }
  return { rows: rows.filter((row): row is Row => typeof row === "object" && row !== null) };
}

export function splunkSiem(tool: RegisteredTool, dispatch: ToolDispatch): Siem {
  return {
    tool: tool.id,
    search: async (spl, window, limit, signal) => {
      if (writes(spl)) return { rows: [], error: "refused: read-only, write commands are blocked" };
      const args = { spl_query: bounded(spl, window), earliest: "0", max_results: Math.min(limit, tool.bounds.maxRows) };
      const result = await dispatch.invoke(tool, args, signal);
      if (!result.ok) {
        const detail = result.failure.kind === "timeout" ? `timed out after ${result.failure.timeoutMs}ms` : result.failure.detail;
        return { rows: [], error: `${result.failure.kind}: ${detail}` };
      }
      return rowsOf(result.rows);
    },
  };
}

export const quote = (value: string): string => `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
