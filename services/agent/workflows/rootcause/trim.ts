import type { ToolResult } from "../../contracts/tool.js";
import type { ToolDispatch } from "../../core/seams.js";

// What one search shows the investigator. The same trimmed result is journaled,
// so a link is proved only from what the model was shown.
export const SHOWN_EVENTS = 25;

// _raw repeats an event its extracted fields already hold. Kept when there are
// too few of those to read the event without it.
const RAW_KEPT_BELOW = 5;

const VALUE_CAP = 1_000;
const CUT = " …[cut]";

// Splunk's own bookkeeping, not the event. _time and _raw are the event's.
const SPLUNK_FIELDS = new Set(["punct", "linecount", "splunk_server", "splunk_server_group", "timestartpos", "timeendpos"]);

function bookkeeping(key: string): boolean {
  if (key === "_time" || key === "_raw") return false;
  return key.startsWith("_") || key.startsWith("date_") || SPLUNK_FIELDS.has(key);
}

function capped(value: unknown): unknown {
  return typeof value === "string" && value.length > VALUE_CAP ? `${value.slice(0, VALUE_CAP)}${CUT}` : value;
}

function trimEvent(event: unknown): unknown {
  if (event === null || typeof event !== "object" || Array.isArray(event)) return event;
  const kept = Object.entries(event as Record<string, unknown>).filter(([key]) => !bookkeeping(key));
  const own = kept.filter(([key]) => !key.startsWith("_")).length;
  return Object.fromEntries(
    kept.filter(([key]) => key !== "_raw" || own < RAW_KEPT_BELOW).map(([key, value]) => [key, capped(value)]),
  );
}

// Room for the row-count line and the tool_result tags wrap puts around the rows.
const WRAP_ROOM = 500;

// Measured as wrap renders it, so a result that fits here is never clamped.
function rendered(row: unknown): number {
  return JSON.stringify([row], null, 2).length;
}

// Only the splunk envelope, `{count, query, results}`, is trimmed. Its own count
// stays the number of events the search found, so the model knows it saw a prefix.
// Events come off the end until the envelope fits the cap; the first always stays.
function trimRow(row: unknown, budget: number): { row: unknown; cut: boolean } {
  if (row === null || typeof row !== "object" || Array.isArray(row)) return { row, cut: false };
  const results = (row as Record<string, unknown>)["results"];
  if (!Array.isArray(results)) return { row, cut: false };
  const events = results.slice(0, SHOWN_EVENTS).map(trimEvent);
  let fitted = { ...(row as Record<string, unknown>), results: events };
  while (events.length > 1 && rendered(fitted) > budget) {
    events.pop();
    fitted = { ...fitted, results: events };
  }
  return { row: fitted, cut: events.length < results.length };
}

// The cap is the run's result_cap, so the journaled result is the whole of what
// the model reads: nothing past it is clamped away from the model but kept for proof.
export function shown(result: ToolResult, cap: number): ToolResult {
  if (!result.ok) return result;
  const budget = Math.max(cap - WRAP_ROOM, 0);
  const rows = result.rows.map((row) => trimRow(row, budget));
  if (rows.every(({ row }, index) => row === result.rows[index])) return result;
  return { ...result, rows: rows.map(({ row }) => row), capped: result.capped || rows.some(({ cut }) => cut) };
}

// Every remote call goes through here; record and finish answer in-process.
export function showing(dispatch: ToolDispatch, cap: number): ToolDispatch {
  return {
    invoke: async (tool, args, signal) => {
      const result = await dispatch.invoke(tool, args, signal);
      return tool.local ? result : shown(result, cap);
    },
  };
}
