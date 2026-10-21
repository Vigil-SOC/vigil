import { ZERO_TOKENS, type TokenCounts } from "../../contracts/budget.js";
import { ProviderError, type Provider, type ProviderEvent, type TurnRequest } from "../../core/provider.js";

export interface ScriptedTurn {
  // What the model asks for on a tool turn.
  calls?: readonly { tool: string; args: string }[];
  content?: string;
  // Sent as separate deltas, so a test can watch the loop accumulate them.
  deltas?: readonly string[];
  // What the model answers with on the emission turn. A string is sent verbatim,
  // so a test can hand back something the schema will reject.
  emit?: unknown;
  fail?: string;
  tokens?: Partial<TokenCounts>;
}

export type ScriptedProvider = Provider & { readonly requests: readonly TurnRequest[] };

// Stands in for the model and nothing else: the registry, scanner, budget, turn cap
// and approval gate are all real, so a test of the loop is a test of the loop.
export function scriptedProvider(script: readonly ScriptedTurn[], model = "scripted/model"): ScriptedProvider {
  const requests: TurnRequest[] = [];
  let step = 0;

  return {
    model,
    provider_type: "scripted",
    requests,
    stream: async function* (request: TurnRequest): AsyncGenerator<ProviderEvent> {
      requests.push(request);
      const next = script[step];
      step += 1;
      if (next === undefined) throw new Error(`the script ran out at turn ${step}`);

      const tokens = { ...ZERO_TOKENS, ...next.tokens };
      // Usage before the failure, as a real surface reports it: what a dying call
      // burned is spend the ledger has to carry.
      if (next.fail !== undefined) {
        yield { type: "usage", tokens };
        throw new ProviderError(next.fail, tokens);
      }

      if (request.emit !== undefined) {
        if (next.emit === undefined) throw new Error(`turn ${step} was asked for an emission the script does not have`);
        yield { type: "text_delta", text: typeof next.emit === "string" ? next.emit : JSON.stringify(next.emit) };
        yield { type: "usage", tokens };
        return;
      }

      // A forced answer round takes the turn scripted as the emission: the same answer, as a call.
      if (request.force !== undefined && next.emit !== undefined && next.calls === undefined) {
        yield { type: "usage", tokens };
        yield { type: "tool_call", call: { id: `c${step}-0`, tool: request.force, args: typeof next.emit === "string" ? next.emit : JSON.stringify(next.emit) } };
        return;
      }

      for (const text of next.deltas ?? (next.content === undefined ? [] : [next.content])) {
        yield { type: "text_delta", text };
      }
      yield { type: "usage", tokens };
      for (const [index, call] of (next.calls ?? []).entries()) {
        yield { type: "tool_call", call: { id: `c${step}-${index}`, ...call } };
      }
    },
  };
}
