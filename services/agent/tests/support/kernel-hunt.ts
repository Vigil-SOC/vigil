import { join } from "node:path";
import { archFor } from "../../plugins/kinds.js";
import { ZERO_TOKENS } from "../../contracts/budget.js";
import type { ToolResult } from "../../contracts/tool.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import type { Message, Provider, ProviderEvent, ToolCall, TurnRequest } from "../../core/provider.js";
import { registryOf } from "../../core/registry.js";
import type { Memory, ToolDispatch } from "../../core/seams.js";
import { buildSpec, type RunSpec } from "../../core/spec.js";
import type { InProcessState } from "../../core/state.js";
import type { HuntKinds } from "../../plugins/hunt/ledger.js";

// A hunt driven by a scripted model that reads what it is shown, shared by the
// kernel hunt tests and the script that records the kernel replay fixture.

export const FIXTURES = join(import.meta.dirname, "..", "fixtures");
export const RUN = "7d3c2d3e-0000-4000-8000-00000000b001";
export const BEACON = "10.0.0.5 is beaconing to 45.77.53.176";

export function specOf(kind: "hunt" | "adjudicate" = "hunt", hypotheses = [BEACON]): RunSpec {
  const entry = archFor(kind);
  const spec = buildSpec({ arch: entry.arch, playbook: join(FIXTURES, "hunt.playbook.yaml"), config: join(FIXTURES, "hunt.config.yaml") }, entry.actions, entry.owned);
  return { ...spec, sections: { ...spec.sections, hypotheses } };
}

// What the model would see: the board in its brief and every tool result since.
export function shown(request: TurnRequest): string {
  return request.messages.map((message: Message) => message.content).join("\n");
}

export function hypothesisIn(request: TurnRequest, status = "active"): string {
  const found = new RegExp(`- (h-[0-9a-f]+) \\[${status}\\]`).exec(shown(request));
  if (found === null) throw new Error("no hypothesis on the board");
  return found[1] as string;
}


type Call = { tool: string; args: unknown };
// A step answers one call, or a batch of them in one response, run in order.
export type Step = (request: TurnRequest) => Call | { calls: Call[] } | { say: string } | { emit: unknown };

// A model that reads the ids off the board and results, as a real one does. Asked for an
// emission (the critic's argument, the narrator's account) it answers by schema.
// `conclusion` is what a hunt that carries a disposition adds to its answer, and `review` what the
// guard's reviewer says to a dismissal.
export function modelOf(steps: readonly Step[], answers: { critic?: unknown; conclusion?: { disposition: string; citations: string[]; severity?: string }; review?: { stance: string; reasons: string; missing: string[] } } = {}): Provider & { requests: TurnRequest[] } {
  const requests: TurnRequest[] = [];
  let at = 0;
  return {
    model: "scripted/model",
    provider_type: "scripted",
    requests,
    stream: async function* (request: TurnRequest): AsyncGenerator<ProviderEvent> {
      requests.push(request);
      if (request.emit !== undefined) {
        const properties = (request.emit["properties"] ?? {}) as Record<string, unknown>;
        const answer = "stance" in properties
          ? (answers.review ?? { stance: "uphold", reasons: "the cited results show it", missing: [] })
          : "benign_explanation_stands" in properties
          ? (answers.critic ?? { benign_explanation: "a backup job", benign_explanation_stands: false, rationale: "the interval is too regular for a backup" })
          : "open_questions" in properties
            ? {
                summary: "the host beaconed; one claim is still open",
                ...("disposition" in properties ? (answers.conclusion ?? { disposition: "malicious", citations: [] }) : {}),
                open_questions: ["is the sync client sanctioned?"],
              }
            : { summary: "the host beaconed", what_happened: "regular outbound flows", next_steps: ["isolate the host"] };
        yield { type: "text_delta", text: JSON.stringify(answer) };
        yield { type: "usage", tokens: ZERO_TOKENS };
        return;
      }
      // Offered nothing to call -- the critic's or the narrator's turn before it is
      // asked for its answer -- it has nothing to do but stop.
      if (request.tools.length === 0) {
        yield { type: "usage", tokens: ZERO_TOKENS };
        return;
      }
      const step = steps[at];
      at += 1;
      if (step === undefined) {
        yield { type: "usage", tokens: ZERO_TOKENS };
        return;
      }
      const next = step(request);
      if ("say" in next) {
        yield { type: "text_delta", text: next.say };
        yield { type: "usage", tokens: ZERO_TOKENS };
        return;
      }
      if ("emit" in next) throw new Error("this model emits only when asked");
      yield { type: "usage", tokens: ZERO_TOKENS };
      const batch = "calls" in next ? next.calls : [next];
      for (const [i, one] of batch.entries()) {
        const call: ToolCall = { id: batch.length === 1 ? `c${at}` : `c${at}.${i}`, tool: one.tool, args: JSON.stringify(one.args) };
        yield { type: "tool_call", call };
      }
    },
  };
}

// The estate: telemetry answers with rows. A local tool -- the hunt's own -- runs here.
export function estate(failing: ReadonlySet<string> = new Set()): ToolDispatch {
  return {
    invoke: async (tool, args): Promise<ToolResult> => {
      if (tool.local) return tool.invoke(args);
      if (failing.has(tool.id)) return { ok: false, failure: { kind: "unavailable", detail: `${tool.id} is down` } };
      return { ok: true, rows: [{ src: "10.0.0.5", dst: "45.77.53.176", interval_s: 300, connections: 412 }], rowCount: 1, capped: false, sourceSystem: tool.id };
    },
  };
}

export function harnessOf(spec: RunSpec, provider: Provider, state: InProcessState<HuntKinds>, dispatch: ToolDispatch = estate(), memory: Memory = nullMemory): Harness<HuntKinds> {
  return { provider, registry: registryOf([], {}), dispatch, budget: budgetOf({ ...spec.budgets, max_calls: 40 }, unmeteredQuota), memory, state };
}

export const finding = (source: string, hypothesis: string) => ({
  source_system: source,
  summary: `every 300s +/- 4s over 6 hours, 412 connections from 10.0.0.5 to 45.77.53.176 (${source})`,
  salience: "anomalous",
  why_notable: "the jitter is too low for a person or a poller",
  payload: JSON.stringify({ interval_s: 300, connections: 412 }),
  supports: [hypothesis],
  rests_on: [{ field: "interval_s", authored: "sensor" }],
});

