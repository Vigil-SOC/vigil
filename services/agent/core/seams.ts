import type { AgentEvent, NewEvent, TerminalPayload } from "../contracts/events.js";
import type { RecallResult } from "../contracts/memory.js";
import type { RegisteredTool, ToolResult } from "../contracts/tool.js";

// The four seams as ports the harness receives rather than builds. Budget is
// already a contract, re-exported so a caller wires all four from one module.
export type { Budget, BudgetLimits, Quota, Refusal, Spend, SpendPayload, TokenCounts } from "../contracts/budget.js";

// One read of memory on exact keys. The run asking travels with it because the far
// side logs every read and an unattributed one tells an auditor nothing, and its
// signal travels with it because a run that has lost its lease must not wait out a
// read whose answer nobody will use.
export interface KeyedRead {
  keys: readonly string[];
  // The freshness boundary, which is the run's own start rather than now: a resumed
  // run and a replay have to sit inside the one the first turn did.
  asOf: string;
  runId: string;
  signal?: AbortSignal;
}

// Null by default: a contract shaped by the component being replaced is the wrong
// contract, so recall returns nothing until something real lands.
export interface Memory {
  recall(cue: string, limit: number): Promise<readonly string[]>;
  // Episodic recall, on exact entity keys. Typed rather than prose because the
  // result is journaled verbatim as the run's recall event and the notes the model
  // reads are rendered from it -- provenance, trust and window do not survive a
  // string, and a rebuild has to present what the run was actually given.
  //
  // Not optional: an implementation that quietly answered nothing would read as an
  // entity nobody has looked at -- see RecallUnavailable for why that is the wrong
  // answer that hides. An implementation with no memory behind it throws, so the
  // caller journals the read as one that did not happen.
  entities(read: KeyedRead): Promise<RecallResult>;
  remember(note: string): Promise<void>;
}

// Snapshots are excluded unless asked for, and `since` reads only what is new.
// The fold runs once per tool turn, so a full read there is quadratic in a run.
export interface ReadOptions {
  since?: number;
  snapshots?: boolean;
}

// Deliberately the ledger repository's public surface, so Postgres satisfies it with
// no adapter. The store assigns seq: a position read before a turn is stale after it.
export interface State<Kinds extends Record<string, unknown> = Record<never, never>> {
  latestSeq(runId: string): Promise<number | null>;
  read(runId: string, opts?: ReadOptions): Promise<AgentEvent<Kinds>[]>;
  append(runId: string, events: readonly NewEvent<Kinds>[]): Promise<number>;
  terminal(runId: string): Promise<TerminalPayload | null>;
}

// How a call reaches its adapter, nothing more; it never scans or renders. The
// signal is the run's: a worker that lost its lease drops the calls in flight.
export interface ToolDispatch {
  invoke(tool: RegisteredTool, args: Record<string, unknown>, signal?: AbortSignal): Promise<ToolResult>;
}
