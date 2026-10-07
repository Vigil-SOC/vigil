import { emptyRecall, recallOf, RECALL_TOOL, type RecallResult } from "../contracts/memory.js";
import type { ToolResult } from "../contracts/tool.js";
import { deadline } from "./remote.js";
import type { Memory } from "./seams.js";

// The keyed read, over the one endpoint that already answers it. The query stays
// on the far side: a second implementation here would be the one that drifts while
// only one of the two is gated.
//
// Not a granted tool, though it travels the tool bridge. The role whose prefix
// carries this never chose to call it, so routing it through the registry would
// put it in the catalogue the model reads and bill it as a call the run made.

export interface RecallOptions {
  url: string;
  token: string;
  timeoutMs?: number;
  fetch?: typeof globalThis.fetch;
}

// A read this slow has already cost more than it can be worth: it opens the run's
// first turn, and what it returns only reorders what to look at first.
const RECALL_TIMEOUT_MS = 10_000;

function refuse(detail: string): Error {
  return new Error(`the keyed read did not answer: ${detail}`);
}

function reasonOf(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

// Every failure here throws, and the caller journals the read as one that did not
// happen rather than as an empty result -- see RecallUnavailable for why those are
// different facts.
function recalled(body: unknown, keys: readonly string[]): RecallResult {
  const result = body as ToolResult;
  if (typeof result !== "object" || result === null) throw refuse("the answer was not a result");
  if (result.ok !== true) {
    const failure = (result as { failure?: { kind?: string; detail?: string } }).failure;
    throw refuse(failure?.detail ?? failure?.kind ?? "the endpoint refused");
  }
  const held = recallOf(result);
  if (held === null) throw refuse(`${keys.length} key(s) asked for, and the answer carried no readable row`);
  return held;
}

// Wraps rather than replaces: the tier underneath carries a parent run's own notes
// forward, which is a different read from an entity's history, and a run that has
// both must keep both.
export function httpRecall(base: Memory, options: RecallOptions): Memory {
  const call = options.fetch ?? globalThis.fetch;

  return {
    ...base,
    entities: async ({ keys, asOf, runId, signal }) => {
      // Known-to-be-none without asking: a read on no keys can only answer this,
      // and the far side would log a read that told nobody anything.
      if (keys.length === 0) return emptyRecall(keys, asOf);

      const timeoutMs = options.timeoutMs ?? RECALL_TIMEOUT_MS;
      const held = deadline(timeoutMs, signal);
      try {
        let response: Response;
        try {
          response = await call(options.url, {
            method: "POST",
            headers: { "content-type": "application/json", authorization: `Bearer ${options.token}` },
            body: JSON.stringify({
              tool: RECALL_TOOL,
              args: {
                entity_keys: [...keys],
                as_of: asOf,
                caller_kind: "run",
                caller_id: runId,
              },
              // Required by the endpoint, both positive (Bounds in
              // core/agents/tools_router.py), and a body without them is refused
              // before the tool runs. One row because the answer is one mapping,
              // which is also what stops _rows slicing the lists inside it.
              bounds: { max_rows: 1, timeout_ms: timeoutMs },
            }),
            signal: held.signal,
          });
        } catch (error) {
          // The run first: a read nobody is waiting for any more did not time out,
          // it was let go of.
          if (signal?.aborted === true) throw refuse(reasonOf(signal.reason));
          if (held.timedOut()) throw refuse(`nothing arrived inside ${timeoutMs}ms`);
          throw refuse(reasonOf(error));
        }

        if (!response.ok) throw refuse(`the endpoint answered ${response.status}`);
        return recalled(await response.json(), keys);
      } finally {
        held.release();
      }
    },
  };
}
