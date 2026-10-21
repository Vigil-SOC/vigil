import type { ProviderEvent, TurnRequest } from "../../core/provider.js";
import { SUBMIT_TOOL } from "../../core/stream.js";

// The schema a request asks to be answered against: the emission's, or the forced submit tool's.
export function schemaAsked(request: TurnRequest): Record<string, unknown> | undefined {
  return request.emit ?? request.tools.find((tool) => tool.id === request.force)?.parameters;
}

// An answer in the form the request asked for: text for an emission, a call for a forced tool.
export function answerEvents(request: TurnRequest, answer: unknown): ProviderEvent[] {
  const body = typeof answer === "string" ? answer : JSON.stringify(answer);
  return request.emit !== undefined
    ? [{ type: "text_delta", text: body }]
    : [{ type: "tool_call", call: { id: "forced", tool: request.force!, args: body } }];
}

// The tools a request offers besides the kernel's submit tool: what a model could search with.
export function callable(request: TurnRequest): TurnRequest["tools"] {
  return request.tools.filter((tool) => tool.id !== SUBMIT_TOOL);
}
