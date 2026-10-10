import type { NewEvent } from "../../contracts/events.js";
import { defineTool, type RegisteredTool } from "../../contracts/tool.js";
import type { Ending, PluginContext, PluginHooks } from "../../core/agent.js";
import type { State } from "../../core/seams.js";
import type { TallyKinds, TallyPayload, TallyVerb } from "../../workflows/tally/vocabulary.js";

// The whole domain: a count kept on the ledger, a tool that adds to it, and an
// ending the model does not control -- the target, reached between two calls.

const MAX_STEP = 5;

function state(ctx: PluginContext): State<TallyKinds> {
  return ctx.harness.state as unknown as State<TallyKinds>;
}

export function targetOf(ctx: PluginContext): number {
  const target = ctx.options.spec.sections["target"];
  return typeof target === "number" ? target : Number.POSITIVE_INFINITY;
}

// Folded rather than held: the last tally event carries where the count got to,
// so a resumed run counts on from there and two folds of one ledger agree.
export async function countOf(ctx: PluginContext): Promise<number> {
  const events = await state(ctx).read(ctx.options.run_id);
  const last = events.filter((event) => event.kind === "tally").at(-1);
  return last === undefined ? 0 : (last.payload as TallyPayload).count;
}

async function journal(ctx: PluginContext, verb: TallyVerb, count: number, note: string): Promise<void> {
  const event: NewEvent<TallyKinds> = { run_id: ctx.options.run_id, run_kind: ctx.options.run_kind, kind: "tally", payload: { verb, count, note } };
  await state(ctx).append(ctx.options.run_id, [event]);
}

// Reads the count off the ledger and answers the new one. It writes nothing: the
// observer journals what the row said, so the count is what the tool returned.
function bump(ctx: PluginContext): RegisteredTool {
  return defineTool(
    {
      id: "bump",
      description: "Add a number to the running count and return the new total.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["by"],
        properties: { by: { type: "integer", minimum: 1, maximum: MAX_STEP } },
      },
      execute: async (args) => {
        const by = args["by"];
        if (typeof by !== "number" || !Number.isInteger(by) || by < 1 || by > MAX_STEP) {
          return { ok: false, failure: { kind: "invalid_args", detail: `by must be an integer from 1 to ${MAX_STEP}` } };
        }
        return { ok: true, rows: [{ count: (await countOf(ctx)) + by }], rowCount: 1, capped: false, sourceSystem: "counter" };
      },
    },
    { maxRows: 1, timeoutMs: 1_000 },
  );
}

export const tallyHooks: PluginHooks = {
  tools: (ctx) => [bump(ctx)],

  // The rows, never the rendering: the count is what the call returned.
  observe: async (ctx, seen) => {
    if (seen.type !== "tool_result" || seen.call.tool !== "bump" || !seen.attempt.result.ok) return;
    const row = seen.attempt.result.rows[0] as { count?: unknown } | undefined;
    if (typeof row?.count !== "number") return;
    await journal(ctx, "TALLY", row.count, "");
    if (row.count >= targetOf(ctx)) return "halt";
  },

  halted: async (ctx): Promise<Ending> => ({ outcome: "completed", reason: `the count reached ${await countOf(ctx)}` }),

  finish: async (ctx, outcome): Promise<Ending | null> => {
    if (outcome.status !== "completed" || outcome.value === null) return null;
    const value = outcome.value as { verb: TallyVerb; note: string };
    await journal(ctx, value.verb, await countOf(ctx), value.note);
    return { outcome: "completed", reason: value.note };
  },
};
