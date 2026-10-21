import { answerEvents, schemaAsked } from "./forced.js";
import { ZERO_TOKENS } from "../../contracts/budget.js";
import type { Provider, ProviderEvent, TurnRequest } from "../../core/provider.js";

// Answers what it was asked rather than what position it is at: a scripted provider
// walks one counter, so two turns at once take each other's entries.
export interface Responding extends Provider {
  // The most turns that were ever in flight together. 1 means nothing overlapped.
  readonly peak: () => number;
  readonly calls: () => number;
}

export interface RespondingOptions {
  // What to emit when asked for an answer. Given the schema, so a test can tell
  // a lead's turn from a worker's without counting.
  emit: (schema: Record<string, unknown>) => unknown;
  // Held open for this many ticks, so a round that overlaps is observably wider
  // than one that does not.
  ticks?: number;
  model?: string;
}

const rest = (ticks: number): Promise<void> =>
  new Promise((done) => {
    let left = ticks;
    const step = () => (left-- <= 0 ? done() : queueMicrotask(step));
    step();
  });

export function respondingProvider(options: RespondingOptions): Responding {
  let inFlight = 0;
  let peak = 0;
  let calls = 0;

  return {
    model: options.model ?? "scripted/model",
    provider_type: "scripted",
    peak: () => peak,
    calls: () => calls,
    stream: async function* (request: TurnRequest): AsyncGenerator<ProviderEvent> {
      inFlight += 1;
      calls += 1;
      peak = Math.max(peak, inFlight);
      try {
        await rest(options.ticks ?? 2);
        const asked = schemaAsked(request);
        const [said] = asked === undefined ? [] : answerEvents(request, options.emit(asked));
        if (said?.type === "text_delta") yield said;
        yield { type: "usage", tokens: ZERO_TOKENS };
        if (said?.type === "tool_call") yield said;
      } finally {
        inFlight -= 1;
      }
    },
  };
}

// The lead is the role whose answer carries an action; every worker in these
// arches answers with results.
export function isLead(schema: Record<string, unknown>): boolean {
  const properties = schema["properties"];
  return typeof properties === "object" && properties !== null && "action" in properties;
}
