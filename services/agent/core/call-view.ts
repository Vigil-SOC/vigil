import type { ToolFailure } from "../contracts/tool.js";

// One row of the Checked tab. cost_usd is the dispatch's spend, repeated on each
// of its calls: a call has no dollar cost of its own. duration_ms is absent when
// the ledger predates timing, or the attempt never reached invoke. iteration is the
// dispatch's, so a reader can tie a call to the move that asked for it; absent on a
// dispatch that carries none. failed is the kind a failed call reports, read off the
// journaled text (nothing else records it); absent when the call succeeded.
export interface CallView {
  question: string;
  tool: string;
  result_length: number;
  cost_usd: number;
  duration_ms?: number;
  iteration?: number;
  failed?: ToolFailure["kind"];
}

interface DispatchCalls {
  iteration?: number;
  query_intent?: string;
  cost_usd?: number;
  calls?: readonly unknown[];
}

// A failed call's result is `failed: <kind> -- <detail>` (renderFailure in core/security.ts),
// after the wrapper's opener. Anchored at the start so rows that merely contain the words do not match.
const FAILURE_KINDS: Record<ToolFailure["kind"], true> = { invalid_args: true, refused: true, timeout: true, unavailable: true, backend_error: true };
const FAILED_CALL = new RegExp(`^(?:<vigil:tool_result[^>]*>\\s*)?failed: (${Object.keys(FAILURE_KINDS).join("|")}) -- `);

export function callViews(dispatches: Iterable<DispatchCalls>): CallView[] {
  const rows: CallView[] = [];
  for (const dispatch of dispatches) {
    const question = dispatch.query_intent ?? "";
    const cost_usd = dispatch.cost_usd ?? 0;
    const iteration = typeof dispatch.iteration === "number" ? dispatch.iteration : undefined;
    for (const call of dispatch.calls ?? []) {
      if (typeof call !== "object" || call === null) continue;
      const record = call as { tool?: unknown; result?: unknown; duration_ms?: unknown };
      const result = typeof record.result === "string" ? record.result : "";
      const duration_ms = typeof record.duration_ms === "number" ? record.duration_ms : undefined;
      const failed = FAILED_CALL.exec(result)?.[1] as ToolFailure["kind"] | undefined;
      rows.push({
        question,
        tool: typeof record.tool === "string" ? record.tool : "",
        result_length: result.length,
        cost_usd,
        ...(duration_ms === undefined ? {} : { duration_ms }),
        ...(iteration === undefined ? {} : { iteration }),
        ...(failed === undefined ? {} : { failed }),
      });
    }
  }
  return rows;
}
