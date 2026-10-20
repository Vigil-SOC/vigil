// Evidence tool registry (spec §12): the read-only guard, timeouts, and the refs a run cites its searches by.
// A ref is the kernel's own (r1, r2, ... by position on the run's ledger): the kernel mints it for a search a
// model makes, and this registry for one the code makes, and both are journaled as `call` events.
import { CALL_REF } from "../../../../core/agent.js";
import { schemaErrors } from "../../../../core/stream.js";
import type { Domain } from "../types.js";

export interface ToolContext {
  allowedDomains: Domain[];
  allDomains: Domain[];
  timeoutMs: number;
  range: [string, string]; // the data's time range
  focusTime?: string; // the move's target time: the default search window is built around it
  beforeMs?: number; // how far back the default window reaches from focusTime
  minCount?: number; // mapping mode: only stats rows seen at least this often come back
}
export interface ToolResult { ok: boolean; data?: string; error?: string }

export interface EvidenceTool<I = any> {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>; // JSON Schema: the kernel offers it, `run` holds the input to it
  readOnly: true;
  run(input: I, ctx: ToolContext): Promise<ToolResult>;
}

export interface ToolCallRecord { toolCallId: string; tool: string; input: unknown; result: ToolResult }

export interface ToolRegistry {
  readonly callCount: number;
  readonly coverage?: Record<string, number>; // searches so far per sourcetype, run-wide
  list(): EvidenceTool[];
  call(name: string, input: unknown, ctx: ToolContext): Promise<ToolCallRecord>;
  inputOf?(ref: string): unknown; // the input a past call ran with (baseline checks)
  outputOf?(ref: string): string | undefined; // what a past call returned (routine checks)
}

const REF = new RegExp(`^${CALL_REF}(\\d+)$`);

// A ref is citable when the run has issued it: positions 1..callCount.
export function issued(tools: Pick<ToolRegistry, "callCount">, ref: string): boolean {
  const m = REF.exec(ref);
  return m !== null && ref === `${CALL_REF}${Number(m[1])}` && Number(m[1]) >= 1 && Number(m[1]) <= tools.callCount;
}

// The sourcetypes a search reads (not the ones it excludes with NOT), counted for coverage.
export function searchedSourcetypes(input: unknown): string[] {
  const spl = String((input as { spl?: unknown })?.spl ?? "");
  return [...new Set([...spl.matchAll(/(NOT\s+)?sourcetype\s*=\s*"?([^"\s|)]+)"?/gi)].filter((m) => !m[1]).map((m) => m[2]!))];
}

// Journals a search the code made itself (the kernel journals the ones a model makes).
export type Journal = (call: ToolCallRecord & { ms: number }) => Promise<void>;

export class Registry implements ToolRegistry {
  private tools = new Map<string, EvidenceTool>();
  // seq: the calls already on the ledger, so the next ref follows them.
  constructor(private seq = 0, private journal?: Journal) {}
  register(t: EvidenceTool): this {
    if ((t as { readOnly: unknown }).readOnly !== true) throw new Error(`tool ${t.name} is not read-only; refused`);
    this.tools.set(t.name, t);
    return this;
  }
  readonly coverage: Record<string, number> = {};
  private inputs = new Map<string, unknown>();
  inputOf(ref: string) { return this.inputs.get(ref); }
  private outputs = new Map<string, string>();
  outputOf(ref: string) { return this.outputs.get(ref); }
  list() { return [...this.tools.values()]; }
  get callCount() { return this.seq; }

  // A call the ledger holds, as the kernel or the code journaled it: citable from here on.
  note(ref: string, input: unknown, output: string): void {
    this.inputs.set(ref, input);
    this.outputs.set(ref, output);
    for (const st of searchedSourcetypes(input)) this.coverage[st] = (this.coverage[st] ?? 0) + 1;
    this.seq = Math.max(this.seq, Number(REF.exec(ref)?.[1] ?? 0));
  }

  // Runs one search: validated, guarded by the tool's timeout. Takes no ref; whoever journaled the call has it.
  async run(name: string, input: unknown, ctx: ToolContext): Promise<ToolResult> {
    const t = this.tools.get(name);
    if (!t) return { ok: false, error: `unknown tool ${name}` };
    const bad = schemaErrors(t.inputSchema, input);
    if (bad) return { ok: false, error: `bad input: ${bad}` };
    let timer: NodeJS.Timeout | undefined;
    try {
      return await Promise.race([
        t.run(input, ctx),
        new Promise<ToolResult>((r) => { timer = setTimeout(() => r({ ok: false, error: `timed out after ${ctx.timeoutMs / 1000}s` }), ctx.timeoutMs + 30_000); }),
      ]);
    } catch (e: any) {
      return { ok: false, error: String(e?.message ?? e) };
    } finally {
      clearTimeout(timer);
    }
  }

  // A search the code makes itself (the rare sweep, a story's sweep): takes the next ref and journals the call.
  async call(name: string, input: unknown, ctx: ToolContext): Promise<ToolCallRecord> {
    const toolCallId = `${CALL_REF}${this.seq + 1}`;
    const started = Date.now();
    const result = await this.run(name, input, ctx);
    this.note(toolCallId, input, result.ok ? result.data ?? "" : "");
    const record = { toolCallId, tool: name, input, result };
    await this.journal?.({ ...record, ms: Date.now() - started });
    return record;
  }
}
