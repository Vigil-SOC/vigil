import type { NewEvent } from "../../../../contracts/events.js";
import type { Budget, Refusal, SpendPayload, State } from "../../../../core/seams.js";
import type { Message, MessageRequest, Messages } from "./messages.js";

// Every model call this loop makes, held against the run's pool and journaled as the
// kernel's own spend: the console's cost, the pool's ceiling and a resume's seed all
// read spend events, so a v3 run is billed exactly like any other.

export class Refused extends Error {
  constructor(readonly refusal: Refusal) {
    super(`the run's budget refused another model call: ${refusal.reason}`);
  }
}

export interface Metering {
  budget: Budget;
  state: State<Record<never, never>>;
  runId: string;
  // The provider the catalog prices the model under, as the spec names it.
  providerType: string;
  role?: string;
}

export function meteredMessages(inner: Messages, m: Metering): Messages {
  return {
    create: async (request: MessageRequest): Promise<Message> => {
      const refusal = await m.budget.beginCall();
      if (refusal !== null) throw new Refused(refusal);
      let message: Message;
      try {
        message = await inner.create(request);
      } catch (error) {
        m.budget.release();
        throw error;
      }
      const tokens = {
        input: message.usage.input_tokens,
        output: message.usage.output_tokens,
        cache_read: message.usage.cache_read_input_tokens ?? 0,
        cache_write: message.usage.cache_creation_input_tokens ?? 0,
      };
      const priced = await m.budget.priceOf(request.model, m.providerType, tokens);
      const payload: SpendPayload = {
        model_id: request.model,
        provider_type: m.providerType,
        role: m.role ?? "lead",
        tokens,
        cost_usd: priced.cost_usd,
        pricing_source: priced.source,
        rates: priced.rates,
        fetched_at: priced.fetched_at,
      };
      m.budget.record(payload);
      await m.state.append(m.runId, [{ run_id: m.runId, run_kind: "root_cause", kind: "spend", payload } as NewEvent<Record<never, never>>]);
      return message;
    },
  };
}
