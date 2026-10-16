import type { StreamEvent } from "../../core/stream.js";

// The vocabulary the console parses, and nothing else. It skips a shape it does
// not know, so adding one is safe and dropping one degrades to silence.
export type ChatEvent =
  | { type: "text"; content: string }
  | { type: "tool_processing" }
  | { type: "context_windowed"; windowed_messages: number; remaining_messages: number }
  | { type: "approval_required"; checkpoint_id: string; tool: string | null; args: string | null }
  | { error: string };

// What the harness reports, said in the console's words. tool_result and usage
// have no shape over there: the ledger carries them and the reader does not.
export function chatEvents(event: StreamEvent<string>): ChatEvent[] {
  switch (event.type) {
    case "text_delta":
      return [{ type: "text", content: event.text }];
    case "tool_call":
      return [{ type: "tool_processing" }];
    case "folded":
      return [{ type: "context_windowed", windowed_messages: event.folded, remaining_messages: event.remaining }];
    case "approval_required":
      return parked(event.pending);
    case "failed":
      return [{ error: event.outcome.reason }];
    default:
      return [];
  }
}

export function sse(event: ChatEvent): string {
  return `data: ${JSON.stringify(event)}\n\n`;
}

// Typed for the console that will render it and said in prose for the one that
// does not yet, so a parked turn never looks like a turn that simply stopped.
function parked(pending: NonNullable<Extract<StreamEvent, { type: "approval_required" }>["pending"]>): ChatEvent[] {
  const { checkpoint_id, tool, args } = pending;
  return [
    { type: "approval_required", checkpoint_id, tool, args },
    { type: "text", content: `\n\n_Waiting on approval to run ${tool ?? "a tool"}._\n\n` },
  ];
}
