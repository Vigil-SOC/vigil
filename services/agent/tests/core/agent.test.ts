import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { archFor } from "../../arch/registry.js";
import { defineTool, type RegisteredTool } from "../../contracts/tool.js";
import type { ResolutionPayload, ReviewPayload, CallPayload } from "../../contracts/events.js";
import type { PluginManifest } from "../../contracts/plugin.js";
import { historyOf, isAgentRun, pluginGrants, runAgent, skillTool, type AgentKinds, type AgentOptions } from "../../core/agent.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import { localDispatch } from "../../core/dispatch.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import { registryOf } from "../../core/registry.js";
import { buildSpec, SpecError, type RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import { investigateKernelReplay, investigateView } from "../../plugins/investigate/view.js";
import { parsePlugin, parseSkill, pluginFor } from "../../plugins/registry.js";
import { quiet } from "../support/plugins.js";
import { scriptedProvider, type ScriptedProvider, type ScriptedTurn } from "../support/scripted-provider.js";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const RUN = "7d3c2d3e-0000-4000-8000-000000000a01";

function specOf(): RunSpec {
  const entry = archFor("investigate");
  return buildSpec({ arch: entry.arch, playbook: join(FIXTURES, "case.playbook.yaml"), config: join(FIXTURES, "case.config.yaml") }, entry.actions);
}

function stub(id: string): RegisteredTool {
  return defineTool(
    {
      id,
      description: `stand-in for ${id}`,
      parameters: { type: "object", additionalProperties: true, properties: {} },
      execute: async () => ({ ok: true, rows: [{ id, seen: `${id} answered` }], rowCount: 1, capped: false, sourceSystem: id }),
    },
    { maxRows: 10, timeoutMs: 1_000 },
  );
}

function harnessOf(spec: RunSpec, plugin: PluginManifest, provider: ScriptedProvider, state: InProcessState<AgentKinds>): Harness<AgentKinds> {
  return {
    provider,
    registry: registryOf([...spec.tools.map((tool) => stub(tool.id)), skillTool(plugin)], pluginGrants(plugin, spec.tools)),
    dispatch: localDispatch,
    budget: budgetOf(spec.budgets, unmeteredQuota),
    memory: nullMemory,
    state,
  };
}

function optionsOf(spec: RunSpec, plugin: PluginManifest, extra: Partial<AgentOptions> = {}): AgentOptions {
  return { run_id: RUN, run_kind: "investigate", spec, plugin, ...extra };
}

const STOP: ScriptedTurn = { calls: [] };

function concluded(disposition: string, citations: string[]): ScriptedTurn {
  return {
    emit: {
      summary: `the lockouts are ${disposition}`,
      disposition,
      confidence: "medium",
      citations,
      gaps: [],
      next_steps: ["reach the owning team"],
    },
  };
}

describe("the kernel runs a plugin to a conclusion", () => {
  const plugin = quiet(pluginFor("investigate"));

  it("journals each call as a numbered step, then the conclusion, review and terminal", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { content: "start from the method", calls: [{ tool: "load_skill", args: '{"name":"investigate-case"}' }] },
      { content: "read the finding", calls: [{ tool: "get_finding", args: '{"finding_id":"f-1"}' }] },
      STOP,
      concluded("malicious", ["r2"]),
    ]);

    const report = await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));

    expect(report.status).toBe("completed");
    expect(report.calls).toBe(2);
    const events = await state.read(RUN);
    // Spend, frames and their content are the kernel's bookkeeping, asserted on their own below.
    expect(events.filter((one) => !["spend", "frame", "content"].includes(one.kind)).map((one) => one.kind)).toEqual([
      "run", "call", "call", "conclusion", "review", "terminal",
    ]);
    const steps = events.filter((one) => one.kind === "call").map((one) => one.payload as CallPayload);
    expect(steps.map((one) => [one.ref, one.turn, one.tool])).toEqual([
      ["r1", 1, "load_skill"],
      ["r2", 2, "get_finding"],
    ]);
    expect(steps[0]?.thought).toBe("start from the method");
    // The ref the model cites is the ref it was shown.
    expect(steps[1]?.result).toContain('ref="r2"');
    expect(steps[0]?.result).toContain("Investigating a case");
    // A disposition nobody guards is not reviewed.
    expect((events.find((one) => one.kind === "review")?.payload as ReviewPayload).stance).toBe("skipped");
    expect(await state.terminal(RUN)).toMatchObject({ outcome: "completed", summary: "the lockouts are malicious" });
    // The engine is stamped on the journaled spec, so a resume and every reader know it.
    expect(isAgentRun(events)).toBe(true);
  });

  it("tells the model its skills and how to cite, in a prefix that does not move", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([{ calls: [{ tool: "get_finding", args: "{}" }] }, STOP, concluded("malicious", ["r1"])]);
    await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));

    const systems = provider.requests.map((request) => request.messages[0]?.content);
    expect(systems[0]).toContain("investigate-case:");
    expect(systems[0]).toContain("Put the refs your answer rests on in citations");
    expect(new Set(systems).size).toBe(1);
  });

  it("lets a guarded answer stand when the reviewer upholds it over a real cited step", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { calls: [{ tool: "get_finding", args: "{}" }] },
      { calls: [{ tool: "propose_hypothesis", args: '{"statement":"a scheduled task locks the account"}' }] },
      { calls: [{ tool: "link_evidence", args: '{"links":[{"evidence_id":"r1","hypothesis_id":"h1","stance":"supports"}]}' }] },
      STOP,
      concluded("benign", ["r1"]),
      { emit: { stance: "uphold", reasons: "the task owner is named", missing: [] } },
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));

    const review = (await state.read(RUN)).find((one) => one.kind === "review")?.payload as ReviewPayload;
    expect(review).toMatchObject({ stance: "uphold", cited: ["r1"], unknown_refs: [], grounded: true, guarded: { field: "disposition", asked: "benign", final: "benign" } });
    expect((await state.terminal(RUN))?.summary).toBe("the lockouts are benign");
    // The reviewer holds no tools and is shown the step it was asked about.
    const asked = provider.requests.at(-1);
    expect(asked?.tools).toEqual([]);
    expect(asked?.messages.map((one) => one.content).join("\n")).toContain('<vigil:record id="r1" cited="true">');
  });

  it("holds an upheld dismissal for review when the record links no cited result to a claim", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { calls: [{ tool: "get_finding", args: "{}" }] },
      STOP,
      concluded("benign", ["r1"]),
      { emit: { stance: "uphold", reasons: "the task owner is named", missing: [] } },
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));

    const review = (await state.read(RUN)).find((one) => one.kind === "review")?.payload as ReviewPayload;
    expect(review).toMatchObject({ stance: "uphold", grounded: false, guarded: { asked: "benign", final: "needs_review" } });
    expect((await state.terminal(RUN))?.summary).toContain("no cited result is linked to a claim on the record");
  });

  it("holds a guarded answer for review when the reviewer finds it unsupported", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { calls: [{ tool: "splunk_search", args: '{"sql":"select 1"}' }] },
      STOP,
      concluded("false_positive", ["r1"]),
      { emit: { stance: "insufficient", reasons: "an empty search is the only support", missing: ["the task's owner"] } },
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));

    const review = (await state.read(RUN)).find((one) => one.kind === "review")?.payload as ReviewPayload;
    expect(review.guarded).toEqual({ field: "disposition", asked: "false_positive", final: "needs_review" });
    expect((await state.terminal(RUN))?.summary).toContain("held as needs_review because the review was insufficient");
  });

  it("does not let an upheld answer stand on refs no step carries", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      STOP,
      concluded("benign", ["r9"]),
      { emit: { stance: "uphold", reasons: "looks fine", missing: [] } },
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));

    const review = (await state.read(RUN)).find((one) => one.kind === "review")?.payload as ReviewPayload;
    expect(review.unknown_refs).toEqual(["r9"]);
    expect(review.guarded?.final).toBe("needs_review");
  });

  it("treats a reviewer that cannot answer as one that did not agree", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { calls: [{ tool: "get_finding", args: "{}" }] },
      STOP,
      concluded("benign", ["r1"]),
      { fail: "the gateway hung up" },
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));

    const review = (await state.read(RUN)).find((one) => one.kind === "review")?.payload as ReviewPayload;
    expect(review.stance).toBe("insufficient");
    // The emission is retried over a folded transcript before it gives up, so the
    // reason is whichever failure came last; that it failed is what matters.
    expect(review.failure).toEqual(expect.any(String));
    expect(review.guarded?.final).toBe("needs_review");
  });

  it("parks on a gated call and resumes from its steps without running them again", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    let ran = 0;
    const counting = (id: string): RegisteredTool =>
      defineTool(
        {
          id,
          description: `counts ${id}`,
          parameters: { type: "object", additionalProperties: true, properties: {} },
          execute: async () => {
            ran += 1;
            return { ok: true, rows: [{ id }], rowCount: 1, capped: false, sourceSystem: id };
          },
        },
        { maxRows: 10, timeoutMs: 1_000 },
      );
    const harness = (provider: ScriptedProvider): Harness<AgentKinds> => ({
      ...harnessOf(spec, plugin, provider, state),
      registry: registryOf([...spec.tools.map((tool) => counting(tool.id)), skillTool(plugin)], pluginGrants(plugin, spec.tools)),
    });

    // case.config.yaml gates case_records, so the run parks there.
    const first = scriptedProvider([{ calls: [{ tool: "get_finding", args: "{}" }, { tool: "case_records", args: '{"case_id":"c-1"}' }] }]);
    const parked = await runAgent(harness(first), optionsOf(spec, plugin));
    expect(parked.status).toBe("waiting_approval");
    expect(parked.calls).toBe(1);
    expect(ran).toBe(1);
    const checkpoint = parked.pending?.checkpoint_id as string;

    const approve: ResolutionPayload = { checkpoint_id: checkpoint, actor: "analyst", answer: "approve", text: "", resolved_at: new Date().toISOString() };
    const second = scriptedProvider([{ calls: [{ tool: "case_records", args: '{"case_id":"c-1"}' }] }, STOP, concluded("malicious", ["r1", "r2"])]);
    const done = await runAgent(harness(second), optionsOf(spec, plugin, { answers: async () => [approve] }));

    expect(done.status).toBe("completed");
    // get_finding ran once, before the park; only the approved call ran after it.
    expect(ran).toBe(2);
    const steps = (await state.read(RUN)).filter((one) => one.kind === "call").map((one) => one.payload as CallPayload);
    expect(steps.map((one) => [one.ref, one.tool])).toEqual([["r1", "get_finding"], ["r2", "case_records"]]);
    // The resumed model read what the first pass learned.
    const resumed = second.requests[0]?.messages ?? [];
    expect(resumed.some((one) => one.role === "tool" && one.content.includes('ref="r1"'))).toBe(true);
  });

  it("ends as budget_exhausted when the budget refuses, without a conclusion", async () => {
    const spec = { ...specOf(), budgets: { ...specOf().budgets, max_calls: 1 } };
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([{ calls: [{ tool: "get_finding", args: "{}" }] }, STOP]);
    const report = await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));

    expect(report.status).toBe("budget_exhausted");
    const kinds = (await state.read(RUN)).map((one) => one.kind);
    expect(kinds).not.toContain("conclusion");
    expect(kinds.at(-1)).toBe("terminal");
  });
});

describe("the console reads a kernel run in the investigate shape", () => {
  it("answers projection and replay with the lead's fields and the kernel's beside them", async () => {
    const plugin = quiet(pluginFor("investigate"));
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { content: "check the finding", calls: [{ tool: "get_finding", args: "{}" }] },
      STOP,
      concluded("malicious", ["r1"]),
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));
    const events = await state.read(RUN);

    const view = investigateView(RUN, events);
    expect(view).toMatchObject({ run_kind: "investigate", status: "terminal", outcome: "completed", dispatched: 1, engine: "plugin" });
    expect(view.decisions.map((one) => one.action)).toEqual(["EXAMINE", "CONCLUDE"]);
    expect(view.calls).toEqual([expect.objectContaining({ tool: "get_finding", question: "check the finding", iteration: 1 })]);
    expect(view.findings[0]?.answer).toMatchObject({ disposition: "malicious" });
    expect(archFor("investigate").projection?.(RUN, events as never)).toMatchObject({ engine: "plugin" });

    const replay = investigateKernelReplay(RUN, events);
    expect(replay.decisions.map((one) => one.action)).toEqual(["EXAMINE", "CONCLUDE"]);
    expect(replay.kernel.turns[0]?.calls[0]?.ref).toBe("r1");
  });
});

describe("historyOf", () => {
  it("rebuilds one assistant turn per model turn, each followed by its results", () => {
    const step = (ref: string, turn: number, thought?: string): CallPayload => ({
      ref, turn, call_id: `c-${ref}`, tool: "t", arguments: "{}", result: `r-${ref}`, ok: true, failure: null, ...(thought ? { thought } : {}),
    });
    const history = historyOf([step("r1", 1, "first"), step("r2", 1), step("r3", 2, "second")]);
    expect(history.map((one) => one.role)).toEqual(["assistant", "tool", "tool", "assistant", "tool"]);
    expect(history[0]).toMatchObject({ content: "first", tool_calls: [{ id: "c-r1" }, { id: "c-r2" }] });
  });
});

describe("plugins load from their directory", () => {
  it("loads investigate with its skill and guard", () => {
    const plugin = quiet(pluginFor("investigate"));
    expect(plugin.skills.map((one) => one.name)).toEqual(["investigate-case"]);
    expect(plugin.guard).toMatchObject({ field: "disposition", fallback: "needs_review" });
    expect(pluginGrants(plugin, specOf().tools)["agent"]).toEqual(
      expect.arrayContaining(["case_records", "get_finding", "splunk_search", "search_findings", "lookup_indicators", "load_skill"]),
    );
  });

  it("refuses a guard value the field's vocabulary does not hold", () => {
    const manifest = `
id: x
description: d
system: s
conclusion:
  summary_field: summary
  citations_field: citations
  schema:
    type: object
    properties:
      summary: { type: string }
      citations: { type: array }
      disposition: { type: string, enum: [a, b] }
guard:
  field: disposition
  guarded: [c]
  fallback: b
  reviewer_prompt: r
`;
    expect(() => parsePlugin(manifest, [])).toThrow(SpecError);
  });

  it("refuses a skill without front matter", () => {
    expect(() => parseSkill("# no front matter")).toThrow(SpecError);
  });

});
