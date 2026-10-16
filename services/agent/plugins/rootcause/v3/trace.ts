// Append-only trace (spec §10.1), held in memory for the run. Every entry carries the correlation id of
// the frontier decision that started its turn; relatedIds are filled once the turn's patch is applied.
import type { Move } from "./frontier.js";

export type TraceType =
  | "decision" | "query" | "tool-output" | "reasoning" | "graph-patch"
  | "confidence-change" | "rejected-evidence" | "rejected-submission" | "refusal" | "stitch" | "exit" | "usage" | "submission";

export interface TraceEntry {
  id: string;
  ts: string;
  runId: string;
  turn: number;
  move?: Move;
  correlationId: string;
  relatedIds: string[];
  type: TraceType;
  payload: unknown;
}

export interface TurnTrace {
  write(type: TraceType, payload: unknown): string;
  relate(ids: string[]): void; // adds ids to every entry of this turn
}

export interface TraceWriter {
  forTurn(turn: number, move: Move | undefined, correlationId: string): TurnTrace;
  byId(id: string): TraceEntry[];
  chronological(): TraceEntry[];
  get(id: string): TraceEntry | undefined;
}

export class MemoryTrace implements TraceWriter {
  private entries: TraceEntry[] = [];
  private seq = 0;
  constructor(private runId: string, private clock: () => Date = () => new Date()) {}

  forTurn(turn: number, move: Move | undefined, correlationId: string): TurnTrace {
    const mine: TraceEntry[] = [];
    return {
      write: (type, payload) => {
        const e: TraceEntry = { id: `T${String(++this.seq).padStart(6, "0")}`, ts: this.clock().toISOString(), runId: this.runId, turn, move, correlationId, relatedIds: [], type, payload };
        this.entries.push(e);
        mine.push(e);
        return e.id;
      },
      relate: (ids) => {
        for (const e of mine) e.relatedIds = [...new Set([...e.relatedIds, ...ids])];
      },
    };
  }

  byId(id: string): TraceEntry[] {
    return this.entries.filter((e) => e.correlationId === id || e.relatedIds.includes(id));
  }
  chronological(): TraceEntry[] {
    return [...this.entries].sort((a, b) => a.ts.localeCompare(b.ts) || a.id.localeCompare(b.id));
  }
  get(id: string): TraceEntry | undefined {
    return this.entries.find((e) => e.id === id);
  }
}
