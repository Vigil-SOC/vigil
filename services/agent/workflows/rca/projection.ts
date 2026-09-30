import type { AgentEvent, CheckpointPayload, ResolutionPayload, TerminalPayload } from "../../contracts/events.js";
import { openCheckpoint, type OpenCheckpoint } from "../../contracts/events.js";
import type { Finding, Hypothesis } from "./findings.js";
import type { Move, RcaKinds, SegmentPayload } from "./vocabulary.js";

// What a reader outside this process is told about a root-cause run: its moves, as a
// hunt's lead decisions are shown, with the searches and notebook writes each stretch
// made; the notebook as it stands; and the report once there is one. Folded off the
// ledger, so two reads of one ledger agree.
export interface TraceSearch {
  spl: string;
  earliest?: string;
  latest?: string;
  rows: number;
}

// The event a finding rests on: the row its evidence value was copied from, as the
// model was shown it, and the search that returned it. What the trace's conclusions
// stand on, rather than everything it happened to look at.
export interface TraceEvidence {
  finding: string;
  value: string;
  segment: number;
  spl: string;
  row: Record<string, unknown> | null;
  raw: string | null;
}

// Rows the model was shown, with where each came from, for finding a cited one again.
interface Seen {
  segment: number;
  spl: string;
  line: string;
}

const canon = (value: string): string =>
  value.replace(/\\"/g, '"').replace(/\\+/g, "\\").replace(/^["'`]+|["'`]+$/g, "").trim().toLowerCase();

export interface TraceMove {
  segment: number;
  action: Move["action"];
  rationale: string;
  next: string | null;
  // FINISH only: whether the gate took the report, and why not when it did not.
  accepted: boolean | null;
  refused: string | null;
  searches: TraceSearch[];
  // Findings written or updated during the stretch, by id.
  wrote: string[];
}

export interface TraceView {
  kind: "root_cause";
  status: "waiting" | "running" | "ended";
  open_checkpoint: OpenCheckpoint | null;
  sources: number;
  searches: number;
  moves: TraceMove[];
  // The stretch in flight: what it has searched so far, before its move is in.
  current: { segment: number; searches: TraceSearch[]; wrote: string[] } | null;
  evidence: TraceEvidence[];
  findings: Finding[];
  hypotheses: Hypothesis[];
  // From the accepted FINISH, or the last one drafted.
  next_steps: string[];
  outcome: TerminalPayload["outcome"] | null;
  reason: string | null;
  report_markdown: string | null;
}

type Event = AgentEvent<RcaKinds>;

export function traceProjection(_runId: string, events: readonly Event[]): TraceView {
  const moves: TraceMove[] = [];
  let searches: TraceSearch[] = [];
  let wrote = new Set<string>();
  let before = new Map<string, string>();
  let notebook: { list: Finding[]; hyps: Hypothesis[] } = { list: [], hyps: [] };
  let sources = 0;
  let count = 0;
  const raised = new Map<string, CheckpointPayload>();
  const answered = new Set<string>();
  let terminal: TerminalPayload | null = null;
  const seen: Seen[] = [];
  let nextSteps: string[] = [];

  for (const event of events) {
    switch (event.kind) {
      case "survey":
        sources = (event.payload as RcaKinds["survey"]).sources.length;
        break;
      case "query": {
        const shown = event.payload as RcaKinds["query"];
        count += 1;
        for (const line of shown.lines) seen.push({ segment: moves.length + 1, spl: shown.spl, line });
        searches.push({ spl: shown.spl, ...(shown.earliest ? { earliest: shown.earliest } : {}), ...(shown.latest ? { latest: shown.latest } : {}), rows: shown.lines.length });
        break;
      }
      case "notebook": {
        notebook = event.payload as RcaKinds["notebook"];
        // A write is any finding whose recorded form changed since the last notebook.
        for (const finding of notebook.list) {
          const now = JSON.stringify(finding);
          if (before.get(finding.id) !== now) wrote.add(finding.id);
          before.set(finding.id, now);
        }
        break;
      }
      case "segment": {
        const segment = event.payload as SegmentPayload;
        const finishing = segment.draft !== null;
        // A ledger written before stretches ended on a move has only the draft: each
        // of its stretches was a report sent to the gate.
        const move: Partial<Move> = segment.move ?? { action: "FINISH", rationale: "" };
        if (finishing && Array.isArray(move.next_steps)) nextSteps = move.next_steps.map(String);
        moves.push({
          segment: segment.segment,
          action: move.action ?? "FINISH",
          rationale: move.rationale ?? "",
          next: move.next ?? null,
          accepted: finishing ? segment.refused === null : null,
          refused: segment.refused,
          searches,
          wrote: [...wrote],
        });
        searches = [];
        wrote = new Set();
        break;
      }
      case "checkpoint": {
        const payload = event.payload as CheckpointPayload;
        raised.set(payload.checkpoint_id, payload);
        break;
      }
      case "resolution":
        answered.add((event.payload as ResolutionPayload).checkpoint_id);
        break;
      case "terminal":
        terminal = event.payload as TerminalPayload;
        break;
    }
  }

  const waiting = [...raised.values()].find((one) => !answered.has(one.checkpoint_id));
  const status = terminal !== null ? "ended" : waiting !== undefined ? "waiting" : "running";
  return {
    kind: "root_cause",
    status,
    open_checkpoint: terminal === null && waiting !== undefined ? openCheckpoint(waiting) : null,
    sources,
    searches: count,
    moves,
    current: terminal === null && (searches.length > 0 || wrote.size > 0) ? { segment: moves.length + 1, searches, wrote: [...wrote] } : null,
    evidence: notebook.list.map((finding) => cited(finding, seen)),
    findings: notebook.list,
    hypotheses: notebook.hyps,
    next_steps: nextSteps,
    outcome: terminal?.outcome ?? null,
    reason: terminal?.reason ?? null,
    report_markdown: terminal?.summary ?? null,
  };
}

// The first row shown that carries the finding's evidence value, which is the row the
// notebook's check accepted it from.
function cited(finding: Finding, seen: readonly Seen[]): TraceEvidence {
  const wanted = canon(finding.evidence);
  const hit = wanted === "" ? undefined : seen.find((one) => canon(one.line).includes(wanted));
  let row: Record<string, unknown> | null = null;
  if (hit !== undefined) {
    try {
      row = JSON.parse(hit.line) as Record<string, unknown>;
    } catch {
      row = null;
    }
  }
  return { finding: finding.id, value: finding.evidence, segment: hit?.segment ?? 0, spl: hit?.spl ?? "", row, raw: hit?.line ?? null };
}
