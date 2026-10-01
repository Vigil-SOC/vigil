// One row of the Checked tab. cost_usd is the dispatch's spend, repeated on each
// of its calls: a call has no dollar cost of its own. duration_ms is absent when
// the ledger predates timing, or the attempt never reached invoke.
export interface CallView {
  question: string;
  tool: string;
  result_length: number;
  cost_usd: number;
  duration_ms?: number;
}

interface DispatchCalls {
  query_intent?: string;
  cost_usd?: number;
  calls?: readonly unknown[];
}

export function callViews(dispatches: Iterable<DispatchCalls>): CallView[] {
  const rows: CallView[] = [];
  for (const dispatch of dispatches) {
    const question = dispatch.query_intent ?? "";
    const cost_usd = dispatch.cost_usd ?? 0;
    for (const call of dispatch.calls ?? []) {
      if (typeof call !== "object" || call === null) continue;
      const record = call as { tool?: unknown; result?: unknown; duration_ms?: unknown };
      const result = typeof record.result === "string" ? record.result : "";
      const duration_ms = typeof record.duration_ms === "number" ? record.duration_ms : undefined;
      rows.push({
        question,
        tool: typeof record.tool === "string" ? record.tool : "",
        result_length: result.length,
        cost_usd,
        ...(duration_ms === undefined ? {} : { duration_ms }),
      });
    }
  }
  return rows;
}
