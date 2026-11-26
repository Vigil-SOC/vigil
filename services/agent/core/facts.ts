import { factsOf, FACTS_TOOL } from "../contracts/facts.js";
import type { ToolResult } from "../contracts/tool.js";
import type { RecallOptions } from "./recall.js";
import { deadline } from "./remote.js";
import type { Memory } from "./seams.js";
import { traceHeaders } from "./tracing.js";

// The run-start fact read (#4742), over the endpoint Recall already uses and on the
// same terms: not a granted tool, so no model sees it in its catalogue or is billed
// for it, the same principal, and a deadline. The read itself -- cache, case rows,
// sources -- stays on the far side, in core/integrations/entity_facts.py.

// The far side holds its own budget (5s by default); this covers it and the hop.
const FACTS_TIMEOUT_MS = 10_000;

function refuse(detail: string): Error {
  return new Error(`the entity-facts read did not answer: ${detail}`);
}

function reasonOf(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

export function httpFacts(base: Memory, options: RecallOptions): Memory {
  const call = options.fetch ?? globalThis.fetch;

  return {
    ...base,
    facts: async ({ keys, caseId, signal, declaredOnly }) => {
      // Known-to-be-none without asking, as Recall does on no keys.
      if (keys.length === 0) return { keys: [], case_id: caseId, facts: [], sources_unavailable: {}, dropped_keys: 0 };

      const timeoutMs = options.timeoutMs ?? FACTS_TIMEOUT_MS;
      const held = deadline(timeoutMs, signal);
      try {
        let response: Response;
        try {
          response = await call(options.url, {
            method: "POST",
            headers: { "content-type": "application/json", authorization: `Bearer ${options.token}`, ...traceHeaders() },
            body: JSON.stringify({
              tool: FACTS_TOOL,
              args: {
                entity_keys: [...keys],
                ...(caseId === null ? {} : { case_id: caseId }),
                ...(declaredOnly === true ? { declared_only: true } : {}),
              },
              // One row: the answer is one mapping (see core/recall.ts).
              bounds: { max_rows: 1, timeout_ms: timeoutMs },
              ...(options.principal === undefined ? {} : { principal: options.principal }),
            }),
            signal: held.signal,
          });
        } catch (error) {
          if (signal?.aborted === true) throw refuse(reasonOf(signal.reason));
          if (held.timedOut()) throw refuse(`nothing arrived inside ${timeoutMs}ms`);
          throw refuse(reasonOf(error));
        }
        if (!response.ok) throw refuse(`the endpoint answered ${response.status}`);
        const result = (await response.json()) as ToolResult;
        if (typeof result !== "object" || result === null) throw refuse("the answer was not a result");
        if (result.ok !== true) {
          const failure = (result as { failure?: { kind?: string; detail?: string } }).failure;
          throw refuse(failure?.detail ?? failure?.kind ?? "the endpoint refused");
        }
        const read = factsOf(result, caseId);
        if (read === null) throw refuse("the answer carried no readable row");
        return read;
      } finally {
        held.release();
      }
    },
  };
}
