// Tool registry (spec §12): read-only guard, timeouts, call ids, and the <tool_output> wrapper that marks
// every result as untrusted data.
import { z } from "zod4";
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
  inputSchema: z.ZodType<I>;
  readOnly: true;
  run(input: I, ctx: ToolContext): Promise<ToolResult>;
}

export interface ToolCallRecord { toolCallId: string; tool: string; input: unknown; result: ToolResult; wrapped: string }

export interface ToolRegistry {
  readonly callCount: number;
  readonly coverage?: Record<string, number>; // searches so far per sourcetype, run-wide
  list(): EvidenceTool[];
  call(name: string, input: unknown, ctx: ToolContext): Promise<ToolCallRecord>;
  inputOf?(toolCallId: string): unknown; // the input a past call ran with (baseline checks)
  outputOf?(toolCallId: string): string | undefined; // what a past call returned (routine checks)
}

export const wrap = (tool: string, id: string, text: string) =>
  `<tool_output tool="${tool}" call="${id}">\n${text.replaceAll("</tool_output>", "<\\/tool_output>")}\n</tool_output>`;

// The sourcetypes a search reads (not the ones it excludes with NOT), counted for coverage.
export function searchedSourcetypes(input: unknown): string[] {
  const spl = String((input as { spl?: unknown })?.spl ?? "");
  return [...new Set([...spl.matchAll(/(NOT\s+)?sourcetype\s*=\s*"?([^"\s|)]+)"?/gi)].filter((m) => !m[1]).map((m) => m[2]))];
}

export class Registry implements ToolRegistry {
  private tools = new Map<string, EvidenceTool>();
  constructor(private seq = 0) {}
  register(t: EvidenceTool): this {
    if ((t as { readOnly: unknown }).readOnly !== true) throw new Error(`tool ${t.name} is not read-only; refused`);
    this.tools.set(t.name, t);
    return this;
  }
  readonly coverage: Record<string, number> = {};
  private inputs = new Map<string, unknown>();
  inputOf(id: string) { return this.inputs.get(id); }
  private outputs = new Map<string, string>();
  outputOf(id: string) { return this.outputs.get(id); }
  list() { return [...this.tools.values()]; }
  get callCount() { return this.seq; }

  async call(name: string, input: unknown, ctx: ToolContext): Promise<ToolCallRecord> {
    const toolCallId = `tc_${++this.seq}`;
    this.inputs.set(toolCallId, input);
    for (const st of searchedSourcetypes(input)) this.coverage[st] = (this.coverage[st] ?? 0) + 1;
    const t = this.tools.get(name);
    let result: ToolResult;
    if (!t) result = { ok: false, error: `unknown tool ${name}` };
    else {
      const parsed = t.inputSchema.safeParse(input);
      if (!parsed.success) result = { ok: false, error: `bad input: ${parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ")}` };
      else {
        let timer: NodeJS.Timeout | undefined;
        try {
          result = await Promise.race([
            t.run(parsed.data, ctx),
            new Promise<ToolResult>((r) => { timer = setTimeout(() => r({ ok: false, error: `timed out after ${ctx.timeoutMs / 1000}s` }), ctx.timeoutMs + 30_000); }),
          ]);
        } catch (e: any) {
          result = { ok: false, error: String(e?.message ?? e) };
        } finally {
          clearTimeout(timer);
        }
      }
    }
    this.outputs.set(toolCallId, result.ok ? result.data ?? "" : "");
    return { toolCallId, tool: name, input, result, wrapped: wrap(name, toolCallId, result.ok ? result.data ?? "" : `ERROR: ${result.error}`) };
  }
}
