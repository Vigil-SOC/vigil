import type { WorkerEvidence } from "./types.js";

const SUMMARY_CAP = 2000;
const WHY_CAP = 500;
const PAYLOAD_CAP = 8000;
const QUESTION_CAP = 300;
const KEY_CAP = 200;

// Named because a reader has to be able to tell a shortened list from a whole one:
// strength.ts rests a verdict on whether the critic argued against everything linked.
export const DROPPED = "_dropped";
export const OMITTED = "fields_omitted";

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

// No cap here: the record is fitted below, which is where what it can afford to lose
// is decided. Keys are scrubbed too -- a delimiter forges a block from either side.
function scrubDeep(value: unknown): unknown {
  if (typeof value === "string") return scrub(value, Number.POSITIVE_INFINITY);
  if (Array.isArray(value)) return value.map(scrubDeep);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, held]) => [scrub(key, KEY_CAP), scrubDeep(held)]));
  }
  return value;
}

// A field cut to less than this costs more to mark than it can carry, so it is
// dropped whole and its room handed to the fields that can use it.
const VIABLE = " [truncated 000000 chars]".length + 8;

// Nothing survived here, which is not the same as an empty object: a caller that
// kept one would be reporting a field it no longer holds.
const GONE = Symbol("gone");

// Whether anything under this node is itself a list. A node with none is a leaf record
// -- a row the estate returned -- and a node with one is structure holding rows.
function nests(node: unknown): boolean {
  if (Array.isArray(node)) return true;
  if (node !== null && typeof node === "object") return Object.values(node).some(nests);
  return false;
}

interface Child {
  // What the whole entry costs, its key and separator included, so dropping one
  // hands back the key as well. Sharing out values alone leaves the keys of dropped
  // fields on the books and starves the survivors.
  want: number;
  overhead: number;
  // The least this child can be kept for. Its whole want when it cannot be cut, so a
  // leaf row is never handed a share it cannot use and dropped as though it were prose.
  least: number;
  fit: (room: number) => unknown;
}

// Two questions, because they have different answers. First which children a record
// can afford at all: smallest first, since what costs least to keep is the flags and
// ids a reader needs and what costs most is prose. Then how much each admitted child
// gets: an equal share, with one wanting less than its share handing the remainder to
// those wanting more, so a small load-bearing field is never cut to pay for a large one.
// ordered keeps a list in the order it arrived: rows are read as a sequence, and an
// analyst handed rows 7 and 8 because they happened to be the shortest is reading
// something the query never returned. Fields have no such order, so the cheapest go
// first there and the flags survive.
function shareAmong(children: readonly Child[], room: number, ordered = false): unknown[] {
  const order = children.map(({ want }, at) => ({ want, at }));
  if (!ordered) order.sort((left, right) => left.want - right.want);
  const held: unknown[] = new Array(children.length).fill(GONE);

  // A child that cannot be afforded even at its least is dropped rather than admitted
  // and gutted: a row reduced to a count of its own missing fields is worse than no
  // row, because it takes up the space a whole one could have had.
  const admitted: number[] = [];
  let floor = 0;
  for (const { at } of order) {
    const cost = children[at]!.least;
    if (floor + cost > room) break;
    floor += cost;
    admitted.push(at);
  }

  let left = room;
  let rest = admitted.length;
  for (const at of admitted) {
    const child = children[at]!;
    const share = Math.min(child.want, Math.max(Math.floor(left / rest), child.least));
    rest -= 1;
    const kept = child.fit(share - child.overhead);
    if (kept === GONE) continue;
    held[at] = kept;
    left -= sizeOf(kept) + child.overhead;
  }
  return held;
}

// Fitted rather than clamped whole: cutting serialised JSON at a character lands
// mid-token, so the parse this used to do could never succeed and every payload over
// the cap collapsed into one unreadable key -- losing its entities, and a null check's
// survives flag with them. Cut once, too: re-cutting a field stacks a second marker,
// and a field at the marker's own length stops shrinking however often it is halved.
//
// entire distinguishes a row from structure. A payload, and a container holding lists,
// may lose parts and still be read for what is left; a row that lost three of its five
// columns is not a shorter row, it is a row that says something the estate never said.
// So a leaf element is carried whole or dropped and its room given to one that fits,
// while an element that holds lists of its own is shrunk from the inside -- which is
// what keeps twenty queries with their query text rather than one query with its rows.
function fit(value: unknown, room: number, entire = false): unknown {
  if (room <= 0) return GONE;
  if (sizeOf(value) <= room) return value;
  if (entire) return GONE;

  if (typeof value === "string") {
    const keep = room - VIABLE;
    return keep < 1 ? GONE : clamp(value, keep);
  }

  if (Array.isArray(value)) {
    const held = shareAmong(
      value.map((one) => {
        const whole = !nests(one);
        const want = sizeOf(one) + 1;
        return {
          want,
          overhead: 1,
          least: whole ? want : 1 + VIABLE,
          fit: (share: number) => fit(one, share, whole),
        };
      }),
      room - 2,
      true,
    );
    const kept = held.filter((one) => one !== GONE);
    return kept.length === 0 ? GONE : kept;
  }

  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value);
    const held = shareAmong(
      entries.map(([key, one]) => {
        const overhead = key.length + 4;
        const want = sizeOf(one) + overhead;
        const whole = typeof one !== "string" && !nests(one) && typeof one !== "object";
        return {
          want,
          overhead,
          least: whole ? want : Math.min(want, overhead + VIABLE),
          fit: (share: number) => fit(one, share),
        };
      }),
      room - 2,
    );

    const out: Record<string, unknown> = {};
    let omitted = 0;
    entries.forEach(([key, one], at) => {
      if (held[at] === GONE) {
        omitted += 1;
        return;
      }
      out[key] = held[at];
      // What went, rather than a shorter list passing for the whole one -- the reading
      // a verdict resting on such a list has to be able to tell apart.
      const was = Array.isArray(one) ? one.length : 0;
      const now = Array.isArray(held[at]) ? (held[at] as unknown[]).length : 0;
      if (was > now) out[`${key}${DROPPED}`] = was - now;
    });
    // A record left holding only the count of what it lost is not a shorter record,
    // it is the space a whole one could have had.
    if (Object.keys(out).length === 0) return GONE;
    if (omitted > 0) out[OMITTED] = omitted;
    return out;
  }

  // A number or a boolean is indivisible: it is carried whole or not at all.
  return GONE;
}

function scrubPayload(payload: Record<string, unknown>): Record<string, unknown> {
  let held = scrubDeep(payload ?? {}) as Record<string, unknown>;
  if (sizeOf(held) <= PAYLOAD_CAP) return held;

  // More than one pass because the first pays for markers and dropped-counts it could
  // not know about until it had written them. It settles in two.
  for (let pass = 0; pass < 3 && sizeOf(held) > PAYLOAD_CAP; pass += 1) {
    const fitted = fit(held, PAYLOAD_CAP);
    if (fitted === GONE) break;
    held = fitted as Record<string, unknown>;
  }

  // Kept so no record reaches the lead unscrubbed. Nothing observed reaches it.
  if (sizeOf(held) > PAYLOAD_CAP) return { truncated: scrub(JSON.stringify(held), PAYLOAD_CAP) };
  return held;
}

// The critic's record, built to fit rather than left for scrubPayload to fit. Its
// fixed fields are what a verdict reads -- survives, and the ids the argument was made
// against -- and its two prose fields are the only ones that can grow. So the prose is
// what gives, and that is a real loss: nothing else journals the critic's turn, so a
// rationale cut here is cut everywhere. It is still the right field to take it from. A
// shortened argument reads as a shorter argument; a shortened id list reads as a critic
// that argued against fewer findings than it did, which is not a smaller claim but a
// different one. The sanitiser stays the backstop it was meant to be.
export function criticPayload(held: Record<string, unknown>): Record<string, unknown> {
  const { rationale, strongest_benign_explanation: benign, ...fixed } = held;
  const room = PAYLOAD_CAP - sizeOf({ ...fixed, rationale: "", strongest_benign_explanation: "" }) - MARGIN;
  if (room <= 0) return held;

  const prose = (text: unknown, share: number): unknown =>
    typeof text === "string" ? clamp(text, Math.max(1, share)) : text;

  // The explanation is the record's headline and the rationale its argument, so the
  // headline is kept whole wherever it fits and the argument takes what is left.
  const first = prose(benign, Math.floor(room / 2));
  return {
    ...fixed,
    strongest_benign_explanation: first,
    rationale: prose(rationale, room - sizeOf(first)),
  };
}

// Room left for the keys and separators scrubPayload would otherwise have to find.
const MARGIN = 200;

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
