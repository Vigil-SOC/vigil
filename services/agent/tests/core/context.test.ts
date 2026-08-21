import { describe, expect, it } from "vitest";
import {
  assemble,
  canonical,
  DEFAULT_FOLD,
  foldHistory,
  prefixBytes,
  prefixOf,
  sizeOf,
  stableTools,
  transientTail,
  type FoldPolicy,
  type Summarise,
} from "../../core/context.js";
import type { Message, ToolSchema } from "../../core/provider.js";

const TOOLS: ToolSchema[] = [
  { id: "search", description: "search", parameters: { type: "object", properties: { q: { type: "string" } } } },
  { id: "bump", description: "bump", parameters: { properties: { n: { type: "number" } }, type: "object" } },
];

function history(count: number): Message[] {
  return Array.from({ length: count }, (_, at) =>
    at % 2 === 0
      ? ({ role: "assistant", content: `turn ${at}`, tool_calls: [] } as Message)
      : ({ role: "tool", call_id: `c${at}`, content: `result ${at}` } as Message),
  );
}

const SUMMARY = (folded: readonly Message[]) => `[${folded.length} folded]`;

describe("the prefix is byte-identical or it is not cached", () => {
  it("produces the same bytes for the same inputs", () => {
    const one = prefixOf("you are a lead", TOOLS, ["a note"]);
    const two = prefixOf("you are a lead", TOOLS, ["a note"]);
    expect(prefixBytes(one)).toBe(prefixBytes(two));
  });

  it("is unmoved by the order tools were registered in", () => {
    const forward = prefixOf("s", TOOLS, []);
    const backward = prefixOf("s", [...TOOLS].reverse(), []);
    expect(prefixBytes(forward)).toBe(prefixBytes(backward));
  });

  it("is unmoved by the key order inside a schema", () => {
    const shuffled: ToolSchema[] = [
      { id: "search", description: "search", parameters: { properties: { q: { type: "string" } }, type: "object" } },
      { id: "bump", description: "bump", parameters: { type: "object", properties: { n: { type: "number" } } } },
    ];
    expect(prefixBytes(prefixOf("s", shuffled, []))).toBe(prefixBytes(prefixOf("s", TOOLS, [])));
  });

  it("moves when the system prompt moves, so a real change is not silently cached", () => {
    expect(prefixBytes(prefixOf("a", TOOLS, []))).not.toBe(prefixBytes(prefixOf("b", TOOLS, [])));
  });

  it("sorts keys at every depth, not only the top", () => {
    expect(JSON.stringify(canonical({ b: { d: 1, c: 2 }, a: 3 }))).toBe('{"a":3,"b":{"c":2,"d":1}}');
  });

  it("leaves array order alone, which is meaning rather than formatting", () => {
    expect(canonical({ xs: [3, 1, 2] })).toEqual({ xs: [3, 1, 2] });
  });

  it("carries the schema through sorted rather than dropping it", () => {
    expect(stableTools(TOOLS).map((tool) => tool.id)).toEqual(["bump", "search"]);
    expect(stableTools(TOOLS)[0]!.parameters).toEqual({ properties: { n: { type: "number" } }, type: "object" });
  });
});

describe("the middle folds and the edges hold", () => {
  it("leaves a history under the cap alone", () => {
    const short = history(10);
    expect(foldHistory(short, SUMMARY)).toEqual({ messages: short, folded: 0 });
  });

  it("keeps the opening and the recent turns, folding only between them", () => {
    const long = history(60);
    const { messages, folded } = foldHistory(long, SUMMARY);

    expect(messages.slice(0, DEFAULT_FOLD.head)).toEqual(long.slice(0, DEFAULT_FOLD.head));
    expect(messages.slice(-DEFAULT_FOLD.tail)).toEqual(long.slice(-DEFAULT_FOLD.tail));
    expect(folded).toBe(60 - DEFAULT_FOLD.head - DEFAULT_FOLD.tail);
  });

  it("replaces the middle with exactly one note", () => {
    const { messages } = foldHistory(history(60), SUMMARY);
    expect(messages).toHaveLength(DEFAULT_FOLD.head + 1 + DEFAULT_FOLD.tail);
  });

  it("never leaves a tool result without the turn that asked for it", () => {
    const long = history(60);
    const { messages, folded } = foldHistory(long, SUMMARY, { head: 1, tail: 8, max_messages: 40 , max_chars: 1_000_000 });

    // head is 1, and message 1 is the tool result answering it. The head grows to
    // keep the pair; nothing is dropped from every slice at once.
    expect(messages.slice(0, 2)).toEqual(long.slice(0, 2));
    expect(messages[2]!.content).toBe("[50 folded]");
    expect(messages).toHaveLength(2 + 1 + 8);
    expect(folded).toBe(50);
  });

  it("loses no message: what is kept plus what is folded is the whole history", () => {
    const long = history(60);
    const { messages, folded } = foldHistory(long, SUMMARY);
    expect(messages.length - 1 + folded).toBe(long.length);
  });
});

describe("the transient tail", () => {
  it("is nothing when there is no working state", () => {
    expect(transientTail("")).toEqual([]);
  });

  it("goes last, after the history", () => {
    const prefix = prefixOf("s", TOOLS, []);
    const { messages } = assemble(prefix, "the task", history(4), "working on it", SUMMARY);
    expect(messages.at(-1)).toEqual({ role: "user", content: "working on it" });
  });

  it("is absent from what a second assembly with no working state produces", () => {
    const prefix = prefixOf("s", TOOLS, []);
    const held = history(4);
    const withTail = assemble(prefix, "the task", held, "volatile", SUMMARY).messages;
    const without = assemble(prefix, "the task", held, "", SUMMARY).messages;

    expect(withTail).toHaveLength(without.length + 1);
    expect(without.some((one) => one.content.includes("volatile"))).toBe(false);
  });

  it("does not move the prefix, whatever the tail holds", () => {
    const prefix = prefixOf("s", TOOLS, ["a note"]);
    const one = assemble(prefix, "the task", history(4), "first", SUMMARY).messages.slice(0, 2);
    const two = assemble(prefix, "the task", history(6), "second", SUMMARY).messages.slice(0, 2);
    expect(one).toEqual(two);
  });
});

// The bug a live run died on: a 400 from the provider, "unexpected tool_use_id
// found in tool_result blocks". boundary() protected the head edge and nothing
// protected the tail, so a fold could open the tail on a tool result whose
// asking turn had just been replaced by the summary note.
describe("a fold never strands a tool result", () => {
  const policy = { head: 2, tail: 8, max_messages: 40 , max_chars: 1_000_000 };
  const summarise = () => "…";

  // Every assistant turn asks for three tools, so a raw index counted back from
  // the end lands mid-run more often than not.
  const history: Message[] = [];
  for (let turn = 0; turn < 20; turn += 1) {
    history.push({ role: "assistant", content: "", tool_calls: [{ id: `c${turn}`, tool: "search", args: "{}" }] });
    for (const part of [0, 1, 2]) history.push({ role: "tool", call_id: `c${turn}-${part}`, content: "rows" });
  }

  it("opens the tail on the turn that asked, not on its results", () => {
    const { messages } = foldHistory(history, summarise, policy);
    const note = messages.findIndex((message) => message.role === "user");

    expect(messages[note + 1]?.role).not.toBe("tool");
  });

  it("leaves no tool result anywhere without an assistant turn before it", () => {
    const { messages } = foldHistory(history, summarise, policy);

    messages.forEach((message, at) => {
      if (message.role !== "tool") return;
      const before = messages.slice(0, at).reverse().find((one) => one.role !== "tool");
      expect(before?.role).toBe("assistant");
    });
  });

  it("still folds something rather than giving up on the whole history", () => {
    expect(foldHistory(history, summarise, policy).folded).toBeGreaterThan(0);
  });
});

describe("a request bounded by weight, not only by turn count", () => {
  // Alternating turns, each tool result `bytes` long: the shape a role granted a bulk
  // query tool actually produces.
  const heavy = (n: number, bytes: number): Message[] =>
    Array.from({ length: n }, (_, i) =>
      i % 2 === 0
        ? ({ role: "assistant", content: `asking ${i}`, tool_calls: [] } as Message)
        : ({ role: "tool", call_id: `c${i}`, content: "x".repeat(bytes) } as Message),
    );

  const note: Summarise = (folded) => `[${folded.length} folded]`;
  const roomy = { head: 2, tail: 4, max_messages: 40, max_chars: 10_000_000 } as FoldPolicy;

  // Twelve messages sit inside max_messages and weigh 60k. That request is the one
  // every write-up died on: the count said it was small.
  it("brings a transcript the count called small under the budget", () => {
    const history = heavy(12, 10_000);
    const policy: FoldPolicy = { head: 2, tail: 4, max_messages: 40, max_chars: 30_000 };

    expect(sizeOf(foldHistory(history, note, roomy).messages)).toBeGreaterThan(policy.max_chars);
    const { messages, folded } = foldHistory(history, note, policy);

    expect(folded).toBeGreaterThan(0);
    expect(sizeOf(messages)).toBeLessThanOrEqual(policy.max_chars);
  });

  // The point of shrinking an edge: it must not shrink past the rule that keeps a tool
  // result with the assistant turn that asked for it. The provider refuses otherwise.
  it("never opens the tail on a tool result while shrinking it", () => {
    const { messages } = foldHistory(heavy(10, 30_000), note, {
      head: 1,
      tail: 6,
      max_messages: 40,
      max_chars: 20_000,
    });

    const summary = messages.findIndex((message) => message.content.startsWith("["));
    expect(summary).toBeGreaterThan(-1);
    expect(messages[summary + 1]?.role).not.toBe("tool");
  });

  // A prefix is not free. A fold that ignored it let the request cross the ceiling with
  // the history well inside its own share of it.
  it("charges the prefix against the same budget the history is folded to", () => {
    const history = heavy(12, 5_000);
    const policy: FoldPolicy = { head: 2, tail: 4, max_messages: 40, max_chars: 40_000 };

    const light = assemble(prefixOf("short", [], []), "task", history, "", note, policy);
    const laden = assemble(prefixOf("s".repeat(25_000), [], []), "task", history, "", note, policy);

    expect(laden.folded).toBeGreaterThan(light.folded);
    expect(sizeOf(laden.messages)).toBeLessThanOrEqual(policy.max_chars);
  });

  // Weight is a ceiling on the fold, not a licence to drop the last turn: a role that
  // cannot see what it just did cannot write it up. One message over the whole budget
  // is result_cap's problem, and the fold says so by refusing to go below one edge.
  it("keeps both edges however heavy they are", () => {
    const { messages } = foldHistory(heavy(8, 30_000), note, {
      head: 1,
      tail: 1,
      max_messages: 40,
      max_chars: 100,
    });

    expect(messages[0]!.content).toBe("asking 0");
    expect(messages.at(-1)!.content).toContain("x");
  });
});
