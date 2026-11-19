import { isIP } from "node:net";
import { KEY_CASE_SENSITIVE_TYPES } from "../../contracts/memory.js";
import { TLDS } from "./tlds.js";
import { ENTITY_TYPES, type Entity, type EntityType, type EvidenceRecord } from "./types.js";

// A payload full of addresses would otherwise let one record dominate the graph.
const PER_RECORD_CAP = 25;

const PATTERNS: readonly [EntityType, RegExp][] = [
  ["arn", /\barn:aws:[a-z0-9-]*:[^\s"',]*/gi],
  ["aws_key", /\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}\b/g],
  ["url", /\bhttps?:\/\/[^\s"'<>]+/gi],
  ["email", /\b[a-z0-9._%+-]+@(?:[a-z0-9-]+\.)+[a-z]{2,24}\b/gi],
  ["hash", /\b(?:[0-9a-f]{64}|[0-9a-f]{40}|[0-9a-f]{32})\b/gi],
  ["ip", /\b\d{1,3}(?:\.\d{1,3}){3}\b/g],
  ["ip", /\b(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}\b/gi],
  ["domain", /\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}\b/gi],
];

const VERSION = /\b(?:v|ver|version|release|build)[\s.:=]*$/i;

export function key(entity: Entity): string {
  return `${entity.type}:${entity.value}`;
}

// Threat intel writes addresses defanged, and threat_intel is now a worker whose
// output feeds this. Normalizing first is cheaper than defanged variants of
export function defang(text: string): string {
  return text
    .replace(/\[\.\]|\(\.\)|\{\.\}/g, ".")
    .replace(/\bh(?:xx|XX)p/gi, "http")
    .replace(/\[:\]/g, ":")
    .replace(/\[at\]/gi, "@");
}

// An indicator's edge trim: JS `trim` plus U+001C-U+001F and U+0085, which Python's
// `strip` takes. Twin of `trim_edges` in core/storage/defang.py; a loop, because a
// regex anchored at the end is quadratic on a long run of spaces.
const EXTRA_EDGE = "\x1c\x1d\x1e\x1f\x85";
const isEdge = (ch: string): boolean => ch.trim() === "" || EXTRA_EDGE.includes(ch);

export function trimEdges(text: string): string {
  let start = 0;
  let end = text.length;
  while (start < end && isEdge(text[start]!)) start += 1;
  while (end > start && isEdge(text[end - 1]!)) end -= 1;
  return text.slice(start, end);
}

// Case-fold so one host is one node. An ARN's resource part and an AWS key id
// are case-significant, and a lowercased key would merge them. Two keep case in
// part (#4542): a process that is an absolute POSIX path, and a url's path,
// query and fragment -- only its scheme and authority are case-folded. `user`
// and `email` are case-folded on purpose, including keys a person types.
// Keeping their case here, in both halves, passes the parity check and splits
// account history. Why, and what that costs, is the Entity Key entry in
// CONTEXT.md. Plain string ops, mirroring `_fold` in core/memory/entity_keys.py;
// `new URL()` re-serialises and the two would drift.
const CASE_SENSITIVE: ReadonlySet<string> = new Set(KEY_CASE_SENSITIVE_TYPES);

function normalize(type: EntityType, value: string): string {
  if (CASE_SENSITIVE.has(type) || (type === "process" && value.startsWith("/"))) return value;
  if (type === "url") {
    const sep = value.indexOf("://");
    const start = sep >= 0 ? sep + 3 : 0;
    const tail = value.slice(start).search(/[/?#]/);
    const end = tail < 0 ? value.length : start + tail;
    return value.slice(0, end).toLowerCase() + value.slice(end);
  }
  return value.toLowerCase();
}

function hasKnownTld(value: string): boolean {
  return TLDS.has(value.toLowerCase().slice(value.lastIndexOf(".") + 1));
}

// Matched loosely then validated, because a pattern tight enough to accept only
// real values is unreadable and still wrong on IPv6 and on TLDs. Both extraction
function wellFormed(type: EntityType, value: string): boolean {
  if (value === "" || value === "-" || value === "null") return false;
  if (type === "ip") return isIP(value) !== 0;
  if (type === "domain" || type === "email") return value.includes(".") && hasKnownTld(value);
  if (type === "hash") return /^[0-9a-f]{32}$|^[0-9a-f]{40}$|^[0-9a-f]{64}$/.test(value);
  return true;
}

// The inverse of `key`: what an operator hands the run modal, and the only shape
// a subject arrives in. Split on the first colon rather than every one, because a
// url value carries its own. An unknown type is refused instead of coerced -- the
// closed set is what stops a typo minting an eleventh kind of entity.
export function parseKey(raw: string): Entity | undefined {
  const at = raw.indexOf(":");
  if (at < 1) return undefined;
  const kind = trimEdges(raw.slice(0, at)).toLowerCase();
  if (!(ENTITY_TYPES as readonly string[]).includes(kind)) return undefined;

  const type = kind as EntityType;
  const value = normalize(type, trimEdges(defang(raw.slice(at + 1))));
  return wellFormed(type, value) ? { type, value } : undefined;
}

export function fromText(raw: string): Entity[] {
  const text = defang(raw);
  const found: Entity[] = [];

  for (const [type, pattern] of PATTERNS) {
    for (const match of text.matchAll(pattern)) {
      const value = normalize(type, match[0].replace(/[.,;)]+$/, ""));
      // A dotted quad after a version word is a release, not a host.
      const before = text.slice(Math.max(0, match.index - 12), match.index);
      if (!wellFormed(type, value) || (type === "ip" && VERSION.test(before))) continue;
      // Defanged and trimmed again after the punctuation trim, as memory's
      // `text_entity_keys` does; nested `[[.]]` only resolves here.
      const final = normalize(type, trimEdges(defang(value)));
      if (final !== "") found.push({ type, value: final });
    }
  }
  return found;
}

// The payload's keys already say what their values are, so structured evidence
// is typed rather than guessed at. This is what stops a Sysmon process name
const KEYS: readonly [EntityType, RegExp][] = [
  ["arn", /arn/i],
  ["hash", /hash|md5|sha\d/i],
  ["url", /\burl|uri\b/i],
  ["email", /email|mail_from|sender/i],
  ["process", /process|image|command|parent|exe\b/i],
  ["user", /user|account|identity|principal|subject/i],
  ["ip", /(?:^|_)ip(?:_|$)|addr|src$|dst$|dest$/i],
  ["host", /hostname/i],
  ["domain", /domain|fqdn|host|query|site|referer/i],
];

// A `host` field holds a machine as often as a website: a bare or unregistered
// name is the machine, memory's `host:` (#4158). An address stays an address.
function refine(type: EntityType, name: string, value: string): EntityType {
  if (type !== "host" && !(type === "domain" && /host/i.test(name))) return type;
  if (isIP(value) !== 0) return "ip";
  return type === "domain" && !(value.includes(".") && hasKnownTld(value)) ? "host" : type;
}

function typeForKey(name: string): EntityType | undefined {
  return KEYS.find(([, pattern]) => pattern.test(name))?.[0];
}

export function fromPayload(payload: unknown, name = ""): Entity[] {
  if (Array.isArray(payload)) return payload.flatMap((item) => fromPayload(item, name));
  if (payload !== null && typeof payload === "object") {
    return Object.entries(payload).flatMap(([field, value]) => fromPayload(value, field));
  }
  if (typeof payload !== "string" && typeof payload !== "number") return [];

  const raw = trimEdges(defang(String(payload)));
  const type = typeForKey(name);
  if (type === undefined) return fromText(raw);

  const value = normalize(type, raw);
  const refined = refine(type, name, value);
  return wellFormed(refined, value) ? [{ type: refined, value }] : [];
}

export function entitiesOf(record: Pick<EvidenceRecord, "summary" | "payload">): Entity[] {
  const found = new Map<string, Entity>();
  for (const entity of [...fromPayload(record.payload), ...fromText(record.summary)]) {
    if (!found.has(key(entity))) found.set(key(entity), entity);
  }
  // A machine the payload names as a host is not also a domain because prose repeats it.
  for (const entity of found.values()) {
    if (entity.type === "host") found.delete(key({ type: "domain", value: entity.value }));
  }
  return [...found.values()].slice(0, PER_RECORD_CAP);
}

export interface EntityNode {
  entity: Entity;
  count: number;
  first_evidence_id: string;
  pairs: Map<string, number>;
}

export interface Neighbour {
  key: string;
  count: number;
}

// Compute-on-read over the entities the records already carry. There is no
// second copy of the graph, so nothing can drift; the port exists so callers ask
export interface EntityGraph {
  node(id: string): EntityNode | undefined;
  nodes(): readonly EntityNode[];
  introduced(evidenceId: string): readonly string[];
  neighbours(id: string): readonly Neighbour[];
  introducedRecurring(record: EvidenceRecord): boolean;
  hasRarePairing(record: EvidenceRecord, max: number): boolean;
}

class Graph implements EntityGraph {
  constructor(
    private readonly byKey: Map<string, EntityNode>,
    private readonly firsts: Map<string, string[]>,
  ) {}

  node(id: string): EntityNode | undefined {
    return this.byKey.get(id);
  }

  nodes(): readonly EntityNode[] {
    return [...this.byKey.values()];
  }

  introduced(evidenceId: string): readonly string[] {
    return this.firsts.get(evidenceId) ?? [];
  }

  neighbours(id: string): readonly Neighbour[] {
    const pairs = this.byKey.get(id)?.pairs ?? new Map<string, number>();
    return [...pairs.entries()]
      .map(([neighbour, count]) => ({ key: neighbour, count }))
      .sort((a, b) => (b.count === a.count ? a.key.localeCompare(b.key) : b.count - a.count));
  }

  private recurs(id: string): boolean {
    return (this.byKey.get(id)?.count ?? 0) > 1;
  }

  // This record was the first sighting of something that went on to recur. A
  // value seen exactly once is a one-off, and telemetry is full of them —
  introducedRecurring(record: EvidenceRecord): boolean {
    return this.introduced(record.evidence_id).some((id) => this.recurs(id));
  }

  // A rare pairing of entities the hunt otherwise knows well. Both must recur,
  // or every pair of one-off addresses reads as rare and the signal is sparsity.
  hasRarePairing(record: EvidenceRecord, max: number): boolean {
    const keys = (record.entities ?? []).map(key).filter((id) => this.recurs(id));
    return keys.some((id) => {
      const pairs = this.byKey.get(id)!.pairs;
      return keys.some((other) => other !== id && (pairs.get(other) ?? 0) <= max);
    });
  }
}

export function buildEntityGraph(ordered: readonly EvidenceRecord[], seed?: Entity): EntityGraph {
  const byKey = new Map<string, EntityNode>();
  const firsts = new Map<string, string[]>();

  // The seed is the hunt's own target. Reporting it as first-seen would promote
  // whichever record happened to mention it first, which says nothing.
  if (seed !== undefined) {
    byKey.set(key(seed), { entity: seed, count: 0, first_evidence_id: "", pairs: new Map() });
  }

  for (const record of ordered) {
    const entities = record.entities ?? [];
    const keys = entities.map(key);
    firsts.set(record.evidence_id, entities.filter((e) => visit(byKey, e, record.evidence_id)).map(key));
    couple(byKey, keys);
  }
  return new Graph(byKey, firsts);
}

// Returns true when this record is the first to mention the entity.
function visit(nodes: Map<string, EntityNode>, entity: Entity, evidenceId: string): boolean {
  const node = nodes.get(key(entity));
  if (node !== undefined) {
    node.count += 1;
    return false;
  }
  nodes.set(key(entity), { entity, count: 1, first_evidence_id: evidenceId, pairs: new Map() });
  return true;
}

function couple(nodes: Map<string, EntityNode>, keys: readonly string[]): void {
  for (const id of keys) {
    const pairs = nodes.get(id)!.pairs;
    for (const other of keys) {
      if (other !== id) pairs.set(other, (pairs.get(other) ?? 0) + 1);
    }
  }
}
