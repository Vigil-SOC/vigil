import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { archFor } from "../../plugins/kinds.js";
import { ZERO_TOKENS } from "../../contracts/budget.js";
import type { AgentEvent } from "../../contracts/events.js";
import type { ToolResult } from "../../contracts/tool.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import type { Message, Provider, ProviderEvent, TurnRequest } from "../../core/provider.js";
import { registryOf } from "../../core/registry.js";
import type { ToolDispatch } from "../../core/seams.js";
import { buildSpec, type RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import { huntView } from "../../plugins/hunt/view.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import { newId } from "../../plugins/hunt/ids.js";
import type { HuntKinds } from "../../plugins/hunt/journal.js";
import { fold, type HuntEvent } from "../../plugins/hunt/ledger.js";
import { BASE_RATE_PROVENANCE, startHunt } from "../../plugins/hunt/opening.js";
import { runHunt } from "../../plugins/hunt/workflow.js";
import { huntSpecFor } from "../support/hunt.js";

// The hunt with a different caller: handed intake's finding and chosen workflow, the agent
// proposes a workflow, never starts one. handoff_ir is never offered; conclude_hunt needs proposed_workflow.
const INTAKE_LINE =
  "intake admitted finding f-4821 (10.128.0.5 beaconing to 198.51.100.7 every 300s) and chose incident-response;" +
  " the host is under command and control";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const RUN = "7d3c2d3e-0000-4000-8000-00000000a001";

function adjudicateSpec(): RunSpec {
  const entry = archFor("adjudicate");
  const spec = buildSpec({ arch: entry.arch, playbook: join(FIXTURES, "hunt.playbook.yaml"), config: join(FIXTURES, "hunt.config.yaml") }, entry.actions, entry.owned);
  return { ...spec, sections: { ...spec.sections, hypotheses: [INTAKE_LINE], hypothesis_loop: true } };
}

function shown(request: TurnRequest): string {
  return request.messages.map((message: Message) => message.content).join("\n");
}

// The intent, read off the board the way a model would: the active hypothesis that
// is not the benign explanation.
function intentIn(request: TurnRequest): string {
  for (const line of shown(request).split("\n")) {
    const found = /^- (h-[0-9a-f]+) \[active\]/.exec(line);
    if (found !== null && !line.includes("the benign explanation")) return found[1] as string;
  }
  throw new Error("no intent on the board");
}

type Step = (request: TurnRequest) => { tool: string; args: unknown } | { say: string };

// Answers the critic and the narrator by schema; otherwise plays the script.
function modelOf(steps: readonly Step[]): Provider & { requests: TurnRequest[] } {
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
        const answer =
          "benign_explanation_stands" in properties
            ? { benign_explanation: "a backup job", benign_explanation_stands: false, rationale: "the interval is too regular for a backup" }
            : { summary: "the host beaconed", what_happened: "regular outbound flows", next_steps: ["contain 10.128.0.5"] };
        yield { type: "text_delta", text: JSON.stringify(answer) };
        yield { type: "usage", tokens: ZERO_TOKENS };
        return;
      }
      const step = request.tools.length === 0 ? undefined : steps[at++];
      yield { type: "usage", tokens: ZERO_TOKENS };
      if (step === undefined) return;
      const next = step(request);
      if ("say" in next) {
        yield { type: "text_delta", text: next.say };
        return;
      }
      yield { type: "tool_call", call: { id: `c${at}`, tool: next.tool, args: JSON.stringify(next.args) } };
    },
  };
}

const estate: ToolDispatch = {
  invoke: async (tool, args): Promise<ToolResult> => {
    if (tool.local) return tool.invoke(args);
    return { ok: true, rows: [{ src: "10.128.0.5", dst: "198.51.100.7", interval_s: 300 }], rowCount: 1, capped: false, sourceSystem: tool.id };
  },
};

const finding = (source: string, hypothesis: string) => ({
  source_system: source,
  summary: `10.128.0.5 -> 198.51.100.7: 412 connections, 300s +/- 4s (${source})`,
  salience: "anomalous",
  why_notable: "regular interval with low variance",
  payload: JSON.stringify({ conn_count: 412, interval_s: 300 }),
  supports: [hypothesis],
  rests_on: [{ field: "interval_s", authored: "sensor" }],
});

describe("an adjudication is stamped and seeded as its own kind", () => {
  it("is stamped with its own kind and seeded with the forward null", async () => {
    const state = new InProcessState<HuntKinds>();
    const runId = newId("run");
    const spec = { ...huntSpecFor({ hypotheses: [INTAKE_LINE], hypothesisLoop: true }), arch: "adjudicate" };
    const ledger = await startHunt(state, new InProcessDirectiveQueue(), runId, spec, "test", "adjudicate");
    await ledger.flush();

    const [opened] = await state.read(runId);
    expect(opened?.run_kind).toBe("adjudicate");
    // The forward base rate, as any hunt gets: the finding is what is on trial,
    // so "this has a legitimate explanation" is the account the intent must beat.
    const [seeded] = [...ledger.projection.hypotheses.values()].filter((h) => h.provenance === BASE_RATE_PROVENANCE);
    expect(seeded?.statement).toContain(`"${INTAKE_LINE}" has a legitimate explanation`);
    expect(seeded?.statement).not.toMatch(/initial-access vector/);
  });
});

describe("a scripted adjudication runs end to end on the kernel", () => {
  it("proves the intent, concludes carrying proposed_workflow, and projects unchanged", async () => {
    const state = new InProcessState<HuntKinds>();
    const spec = adjudicateSpec();
    const provider = modelOf([
      () => ({ tool: "splunk_search", args: { sql: "select * from flows where src = '10.128.0.5'" } }),
      (request) => ({ tool: "record_evidence", args: { results: [finding("network", intentIn(request)), finding("endpoint", intentIn(request))] } }),
      (request) => ({ tool: "validate_hypothesis", args: { hypothesis_id: intentIn(request) } }),
      () => ({ tool: "conclude_hunt", args: { reason: "I agree with intake: containment comes before any further hunting", proposed_workflow: "incident-response" } }),
    ]);
    const harness: Harness<HuntKinds> = {
      provider,
      registry: registryOf([], {}),
      dispatch: estate,
      budget: budgetOf({ ...spec.budgets, max_calls: 40 }, unmeteredQuota),
      memory: nullMemory,
      state,
    };

    const report = await runHunt(harness, { run_id: RUN, run_kind: "adjudicate", spec, actions: [], queue: new InProcessDirectiveQueue() });
    expect(report.status).toBe("completed");

    // Escalation is never offered, and the conclusion must name a workflow.
    const offered = provider.requests.find((request) => request.tools.length > 0)?.tools ?? [];
    expect(offered.map((tool) => tool.id)).not.toContain("handoff_ir");
    const conclude = offered.find((tool) => tool.id === "conclude_hunt");
    expect((conclude?.parameters as { required?: string[] } | undefined)?.required).toContain("proposed_workflow");

    const events = (await state.read(RUN)) as readonly HuntEvent[];
    expect(events[0]?.run_kind).toBe("adjudicate");
    const projection = fold(events);
    const intent = [...projection.hypotheses.values()].find((h) => h.provenance !== BASE_RATE_PROVENANCE);
    expect(intent?.status).toBe("proven");
    expect(projection.hunt.outcome).toBe("completed");
    // The proposal rides on the conclusion the record keeps.
    expect(projection.hunt.termination_reason).toContain("proposed_workflow: incident-response");
    expect(events.some((event) => event.kind === "handoff")).toBe(false);

    // The registry's projection is the kernel hunt's view, handed the ledger the way serve.ts does.
    const erased = events as unknown as readonly AgentEvent<Record<never, never>>[];
    const view = archFor("adjudicate").projection?.(RUN, erased);
    expect(view).toEqual(huntView(RUN, events));
    expect((view as ReturnType<typeof huntView>).hypotheses.find((h) => h.hypothesis_id === intent?.hypothesis_id)?.status).toBe("proven");
  });
});
