import { estimateTokens, type Limiter } from "../../../../core/limiter.js";
import { noVirtualKey, type VirtualKey } from "../../../../core/vk.js";
import type { Message, MessageRequest, Messages } from "./messages.js";

// The loop's calls, carried by Bifrost's Anthropic surface unchanged. That surface passes
// cache_control, adaptive thinking and a forced tool_choice through to the model; the
// OpenAI surface drops adaptive thinking, and without caching a run costs over twice as
// much. Retries, backoff and the rate buckets are the shared Limiter's, so a run of this
// loop draws on the same gateway allowance as every other run.

export interface BifrostOptions {
  // The gateway, not its surface: BIFROST_URL, as the rest of the agent reads it.
  baseUrl: string;
  apiKey?: string;
  limiter: Limiter;
  // The Settings → Budgets key, so Bifrost's budget binds this loop too.
  vk?: VirtualKey;
  // The gateway's own namespacing ("vertex/claude-opus-4-8"); a bare name when absent.
  provider?: string;
  signal?: AbortSignal;
  fetch?: typeof globalThis.fetch;
}

const VERSION = "2023-06-01";

// The Anthropic surface's "overloaded", which the Limiter does not know. A 40-turn run
// meets it; read as the 503 it means, it is retried rather than ending the run.
const OVERLOADED = 529;

// What the Limiter reads a failure by: its status, and Retry-After when the gateway sent one.
export class GatewayError extends Error {
  readonly status: number;
  constructor(
    readonly answered: number,
    readonly headers: Record<string, string>,
    detail: string,
  ) {
    super(`Bifrost answered ${answered}: ${detail}`);
    this.status = answered === OVERLOADED ? 503 : answered;
  }
}

export function bifrostMessages(options: BifrostOptions): Messages {
  const call = options.fetch ?? globalThis.fetch;
  const url = `${options.baseUrl.replace(/\/+$/, "")}/anthropic/v1/messages`;
  const vk = options.vk ?? noVirtualKey;
  return {
    create: async (request: MessageRequest): Promise<Message> => {
      const body = JSON.stringify({ ...request, model: options.provider ? `${options.provider}/${request.model}` : request.model });
      const key = await vk();
      return options.limiter.run(estimateTokens(body), async () => {
        const response = await call(url, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "anthropic-version": VERSION,
            authorization: `Bearer ${options.apiKey ?? "unused"}`,
            ...(key === null ? {} : { "x-bf-vk": key }),
          },
          body,
          ...(options.signal ? { signal: options.signal } : {}),
        });
        if (!response.ok) {
          const retryAfter = response.headers.get("retry-after");
          throw new GatewayError(response.status, retryAfter === null ? {} : { "retry-after": retryAfter }, (await response.text()).slice(0, 500));
        }
        return (await response.json()) as Message;
      });
    },
  };
}
