import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { Outcome } from "../../core/loop.js";
import type { StreamEvent } from "../../core/stream.js";
import { chatEvents, sse, type ChatEvent } from "../../workflows/chat/sse.js";

const CONSOLE = join(import.meta.dirname, "..", "..", "..", "..", "clients", "web", "src", "redesign", "shell", "Chat.tsx");

// What the console acts on, read off the console rather than copied from it: a
// branch added or dropped over there shows up here as a failure.
function handled(): Set<string> {
  const source = readFileSync(CONSOLE, "utf8");
  const types = [...source.matchAll(/ev\.type === '([a-z_]+)'/g)].map((match) => match[1] as string);
  return new Set([...types, ...(source.includes("if (ev.error)") ? ["error"] : [])]);
}

function nameOf(event: ChatEvent): string {
  return "error" in event ? "error" : event.type;
}

const outcome = (reason: string): Outcome<string> =>
  ({ status: "failed", value: null, reason, refusal: null, pending: null, capped: false, transcript: [], calls: [], turns: 1, rejected: [], cost_usd: 0 });

const EVERY: StreamEvent<string>[] = [
  { type: "text_delta", text: "hello" },
  { type: "tool_call", call: { id: "c1", tool: "findings", args: "{}" } },
  { type: "folded", folded: 12, remaining: 18 },
  { type: "approval_required", pending: { checkpoint_id: "apr-1", tool: "isolate", args: "{}" } },
  { type: "failed", outcome: outcome("the budget refused another iteration") },
];

// Sent ahead of the console that renders it (#634), so a parked turn is on the
// wire before anyone can show it. The prose line beside it is what today's build reads.
const AHEAD = ["approval_required"];

describe("the console's vocabulary", () => {
  it("emits nothing the console does not act on, bar what is deliberately ahead of it", () => {
    const emitted = new Set(EVERY.flatMap(chatEvents).map(nameOf));
    expect([...emitted].filter((name) => !handled().has(name) && !AHEAD.includes(name))).toEqual([]);
  });

  it("leaves no branch the console kept and nothing produces", () => {
    const emitted = new Set(EVERY.flatMap(chatEvents).map(nameOf));
    expect([...handled()].filter((name) => !emitted.has(name))).toEqual([]);
  });
});

describe("what the harness says, in the console's words", () => {
  it("relays text verbatim", () => {
    expect(chatEvents({ type: "text_delta", text: "hello" })).toEqual([{ type: "text", content: "hello" }]);
  });

  it("says a tool is running when one is called", () => {
    expect(chatEvents(EVERY[1]!)).toEqual([{ type: "tool_processing" }]);
  });

  it("reports a fold with both counts, because the console renders both", () => {
    expect(chatEvents({ type: "folded", folded: 12, remaining: 18 })).toEqual([
      { type: "context_windowed", windowed_messages: 12, remaining_messages: 18 },
    ]);
  });

  it("says a parked turn twice: once typed, once in prose", () => {
    const said = chatEvents(EVERY[3]!);
    expect(said[0]).toEqual({ type: "approval_required", checkpoint_id: "apr-1", tool: "isolate", args: "{}" });
    // The console does not render the typed event yet, so a reader on today's
    // build sees why the answer stopped rather than an answer that just stopped.
    expect(said[1]).toEqual({ type: "text", content: "\n\n_Waiting on approval to run isolate._\n\n" });
  });

  it("turns a failure into the error the console throws on", () => {
    expect(chatEvents(EVERY[4]!)).toEqual([{ error: "the budget refused another iteration" }]);
  });

  it("says nothing about what the ledger carries and the reader does not", () => {
    const usage: StreamEvent<string> = {
      type: "usage",
      payload: { model_id: "m", provider_type: "bifrost", role: "lead", tokens: { input: 1, output: 1, cache_read: 0, cache_write: 0 }, cost_usd: null, pricing_source: null },
    };
    expect(chatEvents(usage)).toEqual([]);
  });
});

describe("the frame on the wire", () => {
  it("is one data line terminated by a blank one", () => {
    expect(sse({ type: "text", content: "hi" })).toBe('data: {"type":"text","content":"hi"}\n\n');
  });
});
