// A baseline compares an event with its own account's / host's / address's activity over the whole data range
// The model's unusual true/false is not taken on its word: at least one cited search must
// name one of the event's identifying values and span the data range. "Unique to this account" next to other
// accounts is not a baseline (a developer's dozens of routine registrations were once called unusual that way).
import type { CandidateT } from "./model/schemas.js";

export type InputOf = (toolCallId: string) => unknown;
export type OutputOf = (toolCallId: string) => string | undefined;

const WHO_KINDS = new Set(["user", "ip", "host", "arn", "access-key", "principal"]);
const WIDE = 0.9; // the search must cover this share of the data range

// The values a baseline search must name: identities on the event, and its user/ip/host-like artifacts.
// An ARN or path also counts by its last segment (role/session name).
function values(c: CandidateT): string[] {
  const raw = [...(c.identitySignals ?? []).map((s) => s.value), ...c.artifacts.filter((a) => WHO_KINDS.has(a.kind)).map((a) => a.value)];
  return [...new Set(raw.flatMap((v) => [v, v.split(/[/:]/).at(-1) ?? ""]).map((v) => v.trim().toLowerCase()).filter((v) => v.length >= 3))];
}

// A search spans the data range (>= WIDE of it) by its explicit earliest/latest.
export function wideSearch(q: unknown, range: [string, string]): boolean {
  const { earliest, latest } = (q ?? {}) as { earliest?: string; latest?: string };
  const [s, e] = range.map(Date.parse);
  const a = earliest ? Date.parse(earliest) : NaN, z = latest ? Date.parse(latest) : NaN;
  return !Number.isNaN(a) && !Number.isNaN(z) && Math.min(z, e) - Math.max(a, s) >= WIDE * (e - s);
}

// Times seen in a search result's rows: epoch seconds (stats min/max(_time)) and ISO stamps. Only the result
// rows count: the header's "window: <start> → <end>" spans the whole range and made every baseline "routine".
function timesIn(output: string): number[] {
  const text = output.split("\n").filter((l) => l.trimStart().startsWith("{")).join("\n");
  return [...[...text.matchAll(/\b1[6-9]\d{8}(?:\.\d+)?\b/g)].map((m) => Number(m[0]) * 1000), ...[...text.matchAll(/\b20\d\d-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?/g)].map((m) => Date.parse(m[0].replace(" ", "T") + (m[0].length <= 16 ? ":00" : "") + "Z"))].filter((t) => !Number.isNaN(t));
}
const SPREAD = 0.5;

// "Routine" must be shown, not claimed: the cited baseline results must show the activity spread over at least
// half the data range, and over a multi-day range on most of its days. An attacker using a borrowed identity repeats the same pattern within the incident; that
// is not routine (a service account's "history" once turned out to be the attacker's own calls, one day of three).
export function routineShown(c: CandidateT, outputOf: OutputOf | undefined, range: [string, string] | undefined): boolean {
  if (!c.baseline || !outputOf || !range) return !!c.baseline;
  const ts = c.baseline.toolCallIds.flatMap((id) => timesIn(outputOf(id) ?? ""));
  if (!ts.length) return false;
  const [s, e] = range.map(Date.parse);
  if (Math.max(...ts) - Math.min(...ts) < SPREAD * (e - s)) return false;
  // Over a range of several days, routine work spans most of its calendar days. A start-to-end spread alone let two
  // days of a borrowed identity's activity pass (a per-day count stamped at midnight stretched a day and a half
  // to half the range, and the attacker's calls were called routine).
  const days = Math.round((e - s) / 86_400_000);
  if (days < 2) return true;
  // Calendar days from the first to the last time seen (a stats min/max shows only the two ends).
  const day = (t: number) => Math.floor((Math.min(Math.max(t, s), e - 1) - s) / 86_400_000);
  return day(Math.max(...ts)) - day(Math.min(...ts)) + 1 >= Math.ceil(DAYS * days);
}
const DAYS = 0.75; // share of the range's days the activity must span

export function baselineProblem(c: CandidateT, inputOf: InputOf | undefined, range: [string, string] | undefined): string | null {
  const b = c.baseline;
  if (!b || !inputOf || !range) return null;
  const qs = b.toolCallIds.map(inputOf).filter((q): q is { spl?: string; earliest?: string; latest?: string } => !!q && typeof q === "object");
  if (!qs.length) return null; // uncited ids are refused elsewhere
  const [s, e] = range.map(Date.parse);
  const span = e - s;
  const wide = (q: { earliest?: string; latest?: string }) => {
    const a = q.earliest ? Date.parse(q.earliest) : NaN, z = q.latest ? Date.parse(q.latest) : NaN;
    return !Number.isNaN(a) && !Number.isNaN(z) && Math.min(z, e) - Math.max(a, s) >= WIDE * span;
  };
  const vs = values(c);
  const names = (q: { spl?: string }) => !vs.length || vs.some((v) => String(q.spl ?? "").toLowerCase().includes(v));
  if (qs.some((q) => wide(q) && names(q))) return null;
  const who = vs.length ? ` naming the same account/host/address (${vs.slice(0, 3).join(", ")})` : "";
  return `"${c.label.slice(0, 80)}": its baseline must be a cited search${who} over the whole data range (earliest=${range[0]}, latest=${range[1]}) that compares it with that account's own activity on other days, not with other accounts. If that search shows the same kind of activity at other times, set unusual: false (routine, legitimate).`;
}
