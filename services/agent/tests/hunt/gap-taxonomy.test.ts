// CONTEXT.md, Visibility Gap: a tool that timed out or was unavailable. "A refusal or a
// bad argument is a defect and must never be recorded as one." The hunt decided gaps by
// dispatch shape instead, so a worker that could not format its answer, or one parked on
// an approval, was recorded as a blind spot -- floored to anomalous, and three on distinct
// leads gap-locked a hypothesis to inconclusive as though the hunt could not look (#1436).

// The worker cases went with the controller's dispatcher. What remains is the call itself:
// the hunt agent queries the estate, and its observe hook decides what a failure was.
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { archFor } from "../../arch/registry.js";
import { ZERO_TOKENS } from "../../contracts/budget.js";
import type { ToolFailure, ToolResult } from "../../contracts/tool.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import type { Message, Provider, ProviderEvent, ToolCall, TurnRequest } from "../../core/provider.js";
import { registryOf } from "../../core/registry.js";
import type { ToolDispatch } from "../../core/seams.js";
import { buildSpec, type RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import { InProcessDirectiveQueue } from "../../workflows/hunt/directives.js";
import { fold, type HuntEvent, type HuntKinds } from "../../workflows/hunt/ledger.js";
import { buildReport } from "../../workflows/hunt/report.js";
import { DEPLOYMENT_GAP_PROVENANCE, openGaps } from "../../workflows/hunt/strength.js";
import { runHunt } from "../../workflows/hunt/workflow.js";

const RUN = "7d3c2d3e-0000-4000-8000-00000000ca95";
const SPEC: RunSpec = (() => {
  const entry = archFor("hunt");
  const fixtures = join(import.meta.dirname, "..", "fixtures");
  const spec = buildSpec({ arch: entry.arch, playbook: join(fixtures, "hunt.playbook.yaml"), config: join(fixtures, "hunt.config.yaml") }, entry.actions, entry.owned);
  return { ...spec, sections: { ...spec.sections, hypotheses: ["10.0.0.5 is beaconing to 45.77.53.176"] } };
})();

const shown = (request: TurnRequest) => request.messages.map((message: Message) => message.content).join("\n");

function hypothesisIn(request: TurnRequest): string {
  const found = /- (h-[0-9a-f]+) \[active\]/.exec(shown(request));
  if (found === null) throw new Error("no hypothesis on the board");
  return found[1]!;
}

type Step = (request: TurnRequest) => { tool: string; args: unknown } | { say: string };

// As tests/hunt/kernel-hunt.test.ts: a model that reads ids off what it was shown,
// and answers the critic and the narrator by schema when asked.
function modelOf(steps: readonly Step[]): Provider {
  let at = 0;
  return {
    model: "scripted/model",
    provider_type: "scripted",
    stream: async function* (request: TurnRequest): AsyncGenerator<ProviderEvent> {
      if (request.emit !== undefined) {
        const properties = (request.emit["properties"] ?? {}) as Record<string, unknown>;
        const answer = "benign_explanation_stands" in properties
          ? { benign_explanation: "a backup job", benign_explanation_stands: false, rationale: "too regular for a backup" }
          : { summary: "s", what_happened: "w", next_steps: [] };
        yield { type: "text_delta", text: JSON.stringify(answer) };
        yield { type: "usage", tokens: ZERO_TOKENS };
        return;
      }
      const step = request.tools.length === 0 ? undefined : steps[at];
      if (step !== undefined) at += 1;
      const next = step?.(request);
      if (next !== undefined && "say" in next) yield { type: "text_delta", text: next.say };
      yield { type: "usage", tokens: ZERO_TOKENS };
      if (next !== undefined && "tool" in next) {
        const call: ToolCall = { id: `c${at}`, tool: next.tool, args: JSON.stringify(next.args) };
        yield { type: "tool_call", call };
      }
    },
  };
}

// The estate: every remote tool fails the way it is told to, the hunt's own tools run.
function estate(failure: ToolFailure | null): ToolDispatch {
  return {
    invoke: async (tool, args): Promise<ToolResult> => {
      if (tool.local) return tool.invoke(args);
      if (failure !== null) return { ok: false, failure };
      return { ok: true, rows: [{ n: 1 }], rowCount: 1, capped: false, sourceSystem: tool.id };
    },
  };
}

async function hunt(steps: readonly Step[], failure: ToolFailure | null) {
  const state = new InProcessState<HuntKinds>();
  const harness: Harness<HuntKinds> = {
    provider: modelOf(steps),
    registry: registryOf([], {}),
    dispatch: estate(failure),
    budget: budgetOf({ ...SPEC.budgets, max_calls: 40 }, unmeteredQuota),
    memory: nullMemory,
    state,
  };
  await runHunt(harness, { run_id: RUN, run_kind: "hunt", spec: SPEC, actions: [], queue: new InProcessDirectiveQueue() });
  const events = (await state.read(RUN)) as readonly HuntEvent[];
  const projection = fold(events);
  // The arch's roles ask for a capability the fixture deployment binds no tool to,
  // declared at start: a floor on open_gaps that no call moves.
  const declared = [...projection.evidence.values()].filter((record) => record.provenance === DEPLOYMENT_GAP_PROVENANCE).length;
  return { events, projection, declared, gaps: [...projection.evidence.values()].filter((record) => record.provenance === "tool_failure") };
}

const search = (): Step => () => ({ tool: "splunk_search", args: { sql: "select * from flows" } });

const FAILURES: Record<string, ToolFailure> = {
  timeout: { kind: "timeout", timeoutMs: 30_000 },
  unavailable: { kind: "unavailable", detail: "the SIEM is down" },
  refused: { kind: "refused", detail: "not allowed" },
  invalid_args: { kind: "invalid_args", detail: "bad sql" },
};

describe("a tool the estate could not answer is a visibility gap", () => {
  for (const kind of ["timeout", "unavailable"] as const) {
    it(`records a call that failed ${kind} as a blind spot, naming the tool and how`, async () => {
      const { gaps } = await hunt([search(), () => ({ say: "the SIEM did not answer" })], FAILURES[kind]!);

      expect(gaps).toHaveLength(1);
      expect(gaps[0]!.payload).toMatchObject({ tool: "splunk_search", kind });
      // Our gateway's words, not the estate's: nothing in it is investigated.
      expect(gaps[0]!.entities).toEqual([]);
    });
  }

  it("reports it under the run's visibility gaps", async () => {
    const { projection, declared } = await hunt([search(), () => ({ say: "the SIEM did not answer" })], FAILURES["timeout"]!);

    expect(buildReport(projection).gaps).toHaveLength(declared + 1);
  });
});

describe("a refusal or a bad argument is a defect, not a visibility gap", () => {
  for (const kind of ["refused", "invalid_args"] as const) {
    it(`records no gap for a call that failed ${kind}`, async () => {
      const { gaps, projection, declared } = await hunt([search(), () => ({ say: "done" })], FAILURES[kind]!);

      expect(gaps).toEqual([]);
      const [hypothesisId] = [...projection.hypotheses.keys()];
      expect(openGaps(projection, hypothesisId!)).toBe(declared);
    });
  }

  // The hunt's own tools refuse too -- a conclusion the board does not allow -- and
  // that is the hunt being held to its rules, not the estate going dark.
  it("records no gap when one of the hunt's own tools refuses", async () => {
    const { gaps, events } = await hunt([() => ({ tool: "conclude_hunt", args: { reason: "looks fine" } }), () => ({ say: "done" })], null);

    const refused = events.find((event) => event.kind === "call" && (event.payload as { tool: string }).tool === "conclude_hunt");
    expect((refused?.payload as { ok: boolean }).ok).toBe(false);
    expect(gaps).toEqual([]);
  });
});

const finding = (source: string, hypothesisId: string) => ({
  source_system: source,
  summary: `412 connections from 10.0.0.5 to 45.77.53.176 every 300s (${source})`,
  salience: "anomalous",
  why_notable: "the jitter is too low for a person",
  payload: "{}",
  supports: [hypothesisId],
  rests_on: [{ field: "interval_s", authored: "sensor" }],
});

describe("what a blind spot does to a verdict", () => {
  // A call the estate could not answer counts toward the gap lock, as a failed dispatch did.
  it("does not prove a hypothesis while three of the sources it needed timed out", async () => {
    const { projection, declared } = await hunt(
      [
        () => ({ tool: "splunk_search", args: { sql: "select 1" } }),
        () => ({ tool: "search_findings", args: { query: "45.77.53.176" } }),
        () => ({ tool: "lookup_indicators", args: { values: ["45.77.53.176"] } }),
        (request) => ({ tool: "record_evidence", args: { results: [finding("network", hypothesisIn(request)), finding("endpoint", hypothesisIn(request))] } }),
        (request) => ({ tool: "validate_hypothesis", args: { hypothesis_id: hypothesisIn(request) } }),
        () => ({ say: "done" }),
      ],
      FAILURES["timeout"]!,
    );

    const [hypothesis] = [...projection.hypotheses.values()];
    expect(openGaps(projection, hypothesis!.hypothesis_id)).toBe(declared + 3);
    expect(hypothesis!.status).not.toBe("proven");
  });

  // The other half of #1436: a defect on every lead must not lock a claim the
  // evidence carries.
  it("proves a well-supported hypothesis however many calls were refused", async () => {
    const { projection, declared } = await hunt(
      [
        () => ({ tool: "splunk_search", args: { sql: "select 1" } }),
        () => ({ tool: "search_findings", args: { query: "45.77.53.176" } }),
        () => ({ tool: "lookup_indicators", args: { values: ["45.77.53.176"] } }),
        (request) => ({ tool: "record_evidence", args: { results: [finding("network", hypothesisIn(request)), finding("endpoint", hypothesisIn(request))] } }),
        (request) => ({ tool: "validate_hypothesis", args: { hypothesis_id: hypothesisIn(request) } }),
        () => ({ say: "done" }),
      ],
      FAILURES["refused"]!,
    );

    const [hypothesis] = [...projection.hypotheses.values()];
    expect(openGaps(projection, hypothesis!.hypothesis_id)).toBe(declared);
    expect(hypothesis!.status).toBe("proven");
    expect(hypothesis!.resolution_reason ?? "").not.toMatch(/gap-locked|could not look/);
  });
});
