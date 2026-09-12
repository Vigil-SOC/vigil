import type { WorkerEvidence } from "./types.js";

const SUMMARY_CAP = 2000;
const WHY_CAP = 500;
const PAYLOAD_CAP = 8000;
const QUESTION_CAP = 300;

// The digest wraps evidence in <vigil:evidence> so its content cannot read as
// direction. A record that carries the delimiter itself would close that block.
const DELIMITER = /<\/?vigil:/gi;

// ponytail: keyword heuristic, not a classifier — it will miss paraphrase. It
// only ever raises salience (see salienceFloor), so a miss costs the lead
const INSTRUCTION_LIKE = [
  /\bignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\b/i,
  /\bdisregard\s+(all\s+|any\s+)?(previous|prior|above|earlier|instructions)\b/i,
  /\byou\s+(must|should|need to|are required to)\b/i,
  /\b(system|developer)\s+(prompt|message|instruction)/i,
  /\bnew\s+instructions?\b/i,
  /\b(INVESTIGATE|CONCLUDE|ABANDON|HANDOFF_IR|PIVOT|VALIDATE)\b/,
  /^\s*#{1,6}\s/m,
];

export function looksLikeInstruction(text: string): boolean {
  return INSTRUCTION_LIKE.some((pattern) => pattern.test(text));
}

// Truncation is marked rather than silent: a summary that just stops is
// indistinguishable from a finding that was genuinely that short.
function clamp(text: string, cap: number): string {
  return text.length <= cap ? text : `${text.slice(0, cap)} [truncated ${text.length - cap} chars]`;
}

// Exported for the tool boundary: retrieved web content reaches a worker's
// context before any of this runs on the evidence it emits.
export function scrub(text: string, cap: number): string {
  // Control characters other than tab and newline render as nothing, which is
  // exactly what makes them useful for hiding text from a human reviewer.
  const stripped = text.replace(/[\x00-\x08\x0B-\x1F\x7F]/g, "");
  return clamp(stripped.replace(DELIMITER, "<vigil-"), cap);
}

// Questions render as bare markdown under a heading, with no delimiters around
// them, so a newline is what lets one forge a heading of its own.
export function sanitizeQuestion(text: string): string {
  return scrub(text, QUESTION_CAP).replace(/\s+/g, " ").trim();
}

const sizeOf = (value: unknown): number => JSON.stringify(value)?.length ?? 0;

// No cap here: a record is shrunk below by dropping what it can afford to lose.
function scrubDeep(value: unknown): unknown {
  if (typeof value === "string") return scrub(value, Number.POSITIVE_INFINITY);
  if (Array.isArray(value)) return value.map(scrubDeep);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, held]) => [key, scrubDeep(held)]));
  }
  return value;
}

interface Site {
  parent: Record<string, unknown> | unknown[];
  key: string | number;
  value: unknown;
}

function nests(node: unknown): boolean {
  if (Array.isArray(node)) return true;
  if (node !== null && typeof node === "object") return Object.values(node).some(nests);
  return false;
}

// An array holding no further arrays is rows rather than structure, so it is what the
// caller may shrink: dropping from the container above would drop whole queries.
function sitesIn(node: unknown, rows: Site[], prose: Site[]): void {
  const visit = (parent: Record<string, unknown> | unknown[], key: string | number, value: unknown): void => {
    if (typeof value === "string") {
      prose.push({ parent, key, value });
      return;
    }
    if (Array.isArray(value)) {
      if (!value.some(nests)) rows.push({ parent, key, value });
      else value.forEach((held, at) => visit(value, at, held));
      return;
    }
    if (value !== null && typeof value === "object") {
      for (const [held, inner] of Object.entries(value)) visit(value as Record<string, unknown>, held, inner);
    }
  };

  if (node !== null && typeof node === "object") {
    for (const [key, value] of Object.entries(node)) visit(node as Record<string, unknown>, key, value);
  }
}

// Shrunk rather than clamped whole: cutting serialised JSON at a character lands mid-token,
// so the parse this used to do could never succeed and every payload over the cap collapsed
// into one unreadable key -- losing its entities, and a null check's survives flag with them.
function scrubPayload(payload: Record<string, unknown>): Record<string, unknown> {
  const held = scrubDeep(payload ?? {}) as Record<string, unknown>;
  if (sizeOf(held) <= PAYLOAD_CAP) return held;

  for (let guard = 0; sizeOf(held) > PAYLOAD_CAP && guard < 500; guard += 1) {
    const rows: Site[] = [];
    const prose: Site[] = [];
    sitesIn(held, rows, prose);

    // Biggest first, whichever kind. Preferring rows as a class would empty a 25-id list
    // a verdict rests on while pages of prose sat beside it untouched.
    const site = [...rows.filter((one) => (one.value as unknown[]).length > 0), ...prose]
      .sort((left, right) => sizeOf(right.value) - sizeOf(left.value))[0];
    if (site === undefined) break;

    if (Array.isArray(site.value)) {
      const rowsHeld = site.value;
      const keep = Math.floor(rowsHeld.length / 2);
      const dropped = rowsHeld.length - keep;
      rowsHeld.length = keep;
      if (!Array.isArray(site.parent) && typeof site.key === "string") {
        const at = `${site.key}_dropped`;
        site.parent[at] = ((site.parent[at] as number | undefined) ?? 0) + dropped;
      }
      continue;
    }

    const text = site.value as string;
    if (text.length <= 1) break;
    // Halved, not cut to the remaining budget: that would spend the whole loss on one field.
    (site.parent as Record<string, unknown>)[site.key as string] = clamp(text, Math.max(1, Math.floor(text.length / 2)));
  }

  // Kept so no record reaches the lead unscrubbed.
  if (sizeOf(held) > PAYLOAD_CAP) return { truncated: scrub(JSON.stringify(held), PAYLOAD_CAP) };
  return held;
}

// Worker output is model text derived from attacker-controlled telemetry. The
// controller applies this to every dispatcher's records, so no implementation
export function sanitize(record: WorkerEvidence): WorkerEvidence {
  const summary = scrub(record.summary, SUMMARY_CAP);
  const why = scrub(record.why_notable, WHY_CAP);

  return {
    ...record,
    summary,
    why_notable: why,
    payload: scrubPayload(record.payload),
    instruction_like: record.instruction_like || looksLikeInstruction(`${summary}\n${why}`),
  };
}
