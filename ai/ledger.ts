import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { dirname } from "node:path";
import type { Checkpoint, Resolution } from "./checkpoints.js";
import type { HuntReport } from "./report.js";
import {
  SCHEMA_VERSION,
  type DecisionRecord,
  type Digest,
  type Directive,
  type DispatchRecord,
  type EvidenceLink,
  type EvidenceRecord,
  type Handoff,
  type Hypothesis,
  type HuntState,
  type OpenQuestion,
} from "./types.js";

export type PatchTarget = "hunt" | "hypothesis" | "question" | "dispatch";

export type LedgerBody =
  | { kind: "hunt"; hunt: HuntState }
  | { kind: "hypothesis"; hypothesis: Hypothesis }
  | { kind: "question"; question: OpenQuestion }
  | { kind: "evidence"; evidence: EvidenceRecord }
  | { kind: "link"; link: EvidenceLink }
  | { kind: "dispatch"; dispatch: DispatchRecord }
  // The digest sits beside the record, not inside it: it is the snapshot, read by
  // replay and never by the fold, and this is the payload/snapshot split on disk.
  | { kind: "decision"; decision: DecisionRecord; digest_presented: Digest }
  | { kind: "directive"; directive: Directive }
  | { kind: "checkpoint"; checkpoint: Checkpoint }
  | { kind: "resolution"; resolution: Resolution }
  | { kind: "handoff"; handoff: Handoff }
  | { kind: "finalize"; report: HuntReport }
  | { kind: "patch"; target: PatchTarget; id: string; fields: Record<string, unknown> };

export type LedgerEvent = LedgerBody & {
  seq: number;
  ts: string;
  schema_version: number;
};

export interface Projection {
  hunt: HuntState;
  hypotheses: Map<string, Hypothesis>;
  questions: Map<string, OpenQuestion>;
  evidence: Map<string, EvidenceRecord>;
  links: EvidenceLink[];
  dispatches: Map<string, DispatchRecord>;
  decisions: DecisionRecord[];
  directives: Directive[];
  checkpoints: Map<string, Checkpoint>;
  resolutions: Resolution[];
  handoffs: Handoff[];
}

export function newId(prefix: string, bytes = 6): string {
  return `${prefix}-${randomBytes(bytes).toString("hex")}`;
}

export class LedgerError extends Error {}

// Ledgers written before the split carry the digest inside the decision record.
// Hoisting it on read is the same move the store makes between its payload and
// snapshot columns, so one reader serves both shapes.
function hoistSnapshot(event: LedgerEvent): LedgerEvent {
  if (event.kind !== "decision") return event;
  const legacy = event.decision as DecisionRecord & { digest_presented?: Digest };
  if (legacy.digest_presented === undefined) {
    // Neither shape carries one, so the file is damaged: say so here rather than
    // letting replay fault on it several folds later.
    if (event.digest_presented === undefined) {
      throw new LedgerError(`decision at seq ${event.seq} carries no digest`);
    }
    return event;
  }
  const { digest_presented, ...decision } = legacy;
  const ids = digest_presented.recent_evidence.map((record) => record.evidence_id);
  return { ...event, digest_presented, decision: { ...decision, presented_evidence_ids: ids } };
}

// The snapshot side of the log, in decision order. Nothing in the fold calls it.
export function snapshots(log: readonly LedgerEvent[]): Digest[] {
  return log.flatMap((event) => (event.kind === "decision" ? [event.digest_presented] : []));
}

export function parseLog(text: string): LedgerEvent[] {
  return text
    .split("\n")
    .filter((line) => line.trim().length > 0)
    .map((line) => hoistSnapshot(JSON.parse(line) as LedgerEvent));
}

// Append-only JSONL. Every mutation is an event; the projection is a fold and
// is never written back, so the file on disk is the whole audit trail.
export class Ledger {
  readonly path: string;
  private events: LedgerEvent[] = [];
  private view: Projection | null = null;

  private constructor(path: string) {
    this.path = path;
  }

  static create(path: string, hunt: HuntState): Ledger {
    if (existsSync(path)) throw new LedgerError(`ledger already exists: ${path}`);
    mkdirSync(dirname(path), { recursive: true });
    const ledger = new Ledger(path);
    ledger.append({ kind: "hunt", hunt });
    return ledger;
  }

  static open(path: string): Ledger {
    if (!existsSync(path)) throw new LedgerError(`no such ledger: ${path}`);
    const ledger = new Ledger(path);
    ledger.events = parseLog(readFileSync(path, "utf8"));
    return ledger;
  }

  append(body: LedgerBody): LedgerEvent {
    const event: LedgerEvent = {
      ...body,
      seq: this.events.length,
      ts: new Date().toISOString(),
      schema_version: SCHEMA_VERSION,
    };
    appendFileSync(this.path, `${JSON.stringify(event)}\n`);
    this.events.push(event);
    this.view = null;
    return event;
  }

  patch(target: PatchTarget, id: string, fields: Record<string, unknown>): void {
    this.append({ kind: "patch", target, id, fields });
  }

  get projection(): Projection {
    if (this.view === null) this.view = fold(this.events);
    return this.view;
  }

  // The raw events, for replay: folding a prefix rebuilds the state behind any
  // past decision. Readonly because only append may extend the log.
  get log(): readonly LedgerEvent[] {
    return this.events;
  }
}

export function fold(events: readonly LedgerEvent[]): Projection {
  const first = events[0];
  if (first === undefined || first.kind !== "hunt") {
    throw new LedgerError("ledger does not open with a hunt event");
  }

  const view: Projection = {
    hunt: structuredClone(first.hunt),
    hypotheses: new Map(),
    questions: new Map(),
    evidence: new Map(),
    links: [],
    dispatches: new Map(),
    decisions: [],
    directives: [],
    checkpoints: new Map(),
    resolutions: [],
    handoffs: [],
  };

  for (const event of events.slice(1)) {
    switch (event.kind) {
      case "hunt":
        throw new LedgerError(`second hunt event at seq ${event.seq}`);
      case "hypothesis":
        view.hypotheses.set(event.hypothesis.hypothesis_id, structuredClone(event.hypothesis));
        break;
      case "question":
        view.questions.set(event.question.question_id, structuredClone(event.question));
        break;
      case "evidence":
        view.evidence.set(event.evidence.evidence_id, structuredClone(event.evidence));
        break;
      case "link":
        view.links.push(structuredClone(event.link));
        break;
      case "dispatch":
        view.dispatches.set(event.dispatch.dispatch_id, structuredClone(event.dispatch));
        break;
      case "decision":
        view.decisions.push(structuredClone(event.decision));
        break;
      case "directive":
        view.directives.push(structuredClone(event.directive));
        break;
      case "checkpoint":
        view.checkpoints.set(event.checkpoint.checkpoint_id, structuredClone(event.checkpoint));
        break;
      case "resolution":
        view.resolutions.push(structuredClone(event.resolution));
        break;
      case "handoff":
        view.handoffs.push(structuredClone(event.handoff));
        break;
      case "finalize":
        // A deliverable, not state: the report is derived from the fold, so the
        // fold must never derive anything from it.
        break;
      case "patch":
        applyPatch(view, event);
        break;
    }
  }
  return view;
}

function applyPatch(view: Projection, event: LedgerEvent & { kind: "patch" }): void {
  const target =
    event.target === "hunt"
      ? view.hunt
      : event.target === "hypothesis"
        ? view.hypotheses.get(event.id)
        : event.target === "question"
          ? view.questions.get(event.id)
          : view.dispatches.get(event.id);

  if (target === undefined) {
    throw new LedgerError(`patch at seq ${event.seq} targets unknown ${event.target} ${event.id}`);
  }
  Object.assign(target, event.fields);
}
