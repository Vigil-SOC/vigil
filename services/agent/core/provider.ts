import { ZERO_TOKENS, type TokenCounts } from "../contracts/budget.js";

export interface ToolCall {
  id: string;
  tool: string;
  args: string;
}

// The harness's own message shape rather than a vendor's, so the loop imports no
// SDK and a second surface or a scripted provider is a drop-in.
export type Message =
  | { role: "system" | "user"; content: string }
  | { role: "assistant"; content: string; tool_calls: readonly ToolCall[] }
  | { role: "tool"; call_id: string; content: string };

export interface ToolSchema {
  id: string;
  description: string;
  parameters: Record<string, unknown>;
}

export interface TurnRequest {
  messages: readonly Message[];
  tools: readonly ToolSchema[];
  // Set on the emission turn: answer against this schema, offer no tools. Combining
  // tools with a strict output format degrades unpredictably across providers.
  emit?: Record<string, unknown>;
  signal?: AbortSignal;
}

// One model call assembled from its stream. Nothing on the wire returns this; it
// is what a consumer holds once the stream has ended.
export interface Turn {
  content: string;
  tool_calls: readonly ToolCall[];
  tokens: TokenCounts;
}

// Usage arrives before the stream ends, so a call that dies afterwards has still
// reported what it spent. Tool calls come last, once the model has asked for them.
export type ProviderEvent =
  | { type: "text_delta"; text: string }
  | { type: "usage"; tokens: TokenCounts }
  | { type: "tool_call"; call: ToolCall };

// Exactly one model call, streamed. The loop belongs to the harness, which keeps
// the turn cap, approval gate and injection scan out of a component a workflow can swap.
export interface Provider {
  readonly model: string;
  readonly provider_type: string;
  // The sampling parameters every call is sent with, journaled on each frame so a
  // replay knows what the model was asked under. Absent is the provider's defaults.
  readonly sampling?: Readonly<Record<string, unknown>>;
  stream(request: TurnRequest): AsyncIterable<ProviderEvent>;
}

// Carries what was already burned, so a call that fails does not drop spend the
// budget still has to account for.
export class ProviderError extends Error {
  constructor(
    message: string,
    readonly tokens: TokenCounts = ZERO_TOKENS,
  ) {
    super(message);
  }
}
