import type { ToolResult } from "../../contracts/tool.js";
import type { LocalExecutor } from "../../tools/local.js";
import type { State } from "../../core/seams.js";
import { fold, type HuntKinds } from "./ledger.js";

// Whole records are dropped at the budget rather than one cut mid-JSON -- the same
// rule the EXPAND action applies, because it is the same question asked mid-turn.
const EXPANSION_BUDGET = 12_000;

// The lead's mid-turn expansion: it cites ids from the digest and gets the raw
// payloads back. Local, because the answer is this run's own record.
export function expandFrom(state: State<HuntKinds>, runId: string): LocalExecutor {
  return async (args): Promise<ToolResult> => {
    const asked = args["evidence_ids"];
    if (!Array.isArray(asked) || asked.length === 0) {
      return { ok: false, failure: { kind: "invalid_args", detail: "expand needs evidence_ids, a non-empty array of ids from the digest" } };
    }

    const projection = fold(await state.read(runId));
    const rows: Array<Record<string, unknown>> = [];
    const dropped: string[] = [];
    let budget = EXPANSION_BUDGET;

    for (const id of asked.map(String)) {
      const record = projection.evidence.get(id);
      // Named rather than skipped: an id the ledger does not hold means the lead
      // cited something it was not shown, which it should learn rather than guess at.
      if (record === undefined) {
        rows.push({ evidence_id: id, expanded: false, reason: "no record on this run's ledger holds that id" });
        continue;
      }
      const payload = JSON.stringify(record.payload, null, 2);
      if (payload.length > budget) {
        dropped.push(id);
        continue;
      }
      budget -= payload.length;
      rows.push({ evidence_id: id, expanded: true, payload });
    }

    for (const id of dropped) {
      rows.push({ evidence_id: id, expanded: false, reason: "too large to expand alongside the others; ask for it on its own" });
    }

    return { ok: true, rows, rowCount: rows.length, capped: false, sourceSystem: "ledger" };
  };
}
