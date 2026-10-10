import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { archFor } from "../../plugins/kinds.js";
import { defineTool, type RegisteredTool, type ToolResult } from "../../contracts/tool.js";
import type {
  CallPayload,
  ChildPayload,
  ConclusionPayload,
  FramePayload,
  ResolutionPayload,
  ReviewPayload,
  RunPayload,
} from "../../contracts/events.js";
import type { PluginManifest } from "../../contracts/plugin.js";
import { pluginGrants, runAgent, skillTool, type AgentKinds, type AgentOptions } from "../../core/agent.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import { childRunId } from "../../core/frames.js";
import { localDispatch } from "../../core/dispatch.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import { classify, recordOf } from "../../core/record.js";
import { registryOf } from "../../core/registry.js";
import { replayFrames, trajectoryOf } from "../../core/replay.js";
import { buildSpec, type RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import { inputHash } from "../../core/stream.js";
import { pluginFor } from "../../plugins/registry.js";
import { quiet } from "../support/plugins.js";
import { scriptedProvider, type ScriptedProvider, type ScriptedTurn } from "../support/scripted-provider.js";

// What the kernel owes every plugin: a hypothesis record, a frame per model call, classified
// results, judges on their own ledgers, and a replay off the ledger alone.

const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const RUN = "7d3c2d3e-0000-4000-8000-000000000b01";

function specOf(): RunSpec {
  const entry = archFor("investigate");
  const spec = buildSpec({ arch: entry.arch, playbook: join(FIXTURES, "case.playbook.yaml"), config: join(FIXTURES, "case.config.yaml") }, entry.actions);
  // Room for every model call these scripts make, the reviewer's included.
  return { ...spec, budgets: { ...spec.budgets, max_calls: 50 } };
}

function tool(id: string, result: () => ToolResult = () => ({ ok: true, rows: [{ id, seen: `${id} answered` }], rowCount: 1, capped: false, sourceSystem: id })): RegisteredTool {
  return defineTool(
    { id, description: `stand-in for ${id}`, parameters: { type: "object", additionalProperties: true, properties: {} }, execute: async () => result() },
    { maxRows: 10, timeoutMs: 1_000 },
  );
}

function harnessOf(spec: RunSpec, plugin: PluginManifest, provider: ScriptedProvider, state: InProcessState<AgentKinds>, tools?: RegisteredTool[]): Harness<AgentKinds> {
  return {
    provider,
    registry: registryOf([...(tools ?? spec.tools.map((one) => tool(one.id))), skillTool(plugin)], pluginGrants(plugin, spec.tools)),
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

function concluded(disposition: string, citations: string[], gaps: string[] = []): ScriptedTurn {
  return { emit: { summary: `the lockouts are ${disposition}`, disposition, confidence: "medium", citations, gaps, next_steps: [] } };
}

const call = (tool: string, args: unknown = {}): { tool: string; args: string } => ({ tool, args: JSON.stringify(args) });

// A dismissal that goes through every part: a record, a guarded answer, a judge.
const DISMISSAL: ScriptedTurn[] = [
  { content: "read the finding first", calls: [call("get_finding", { finding_id: "f-1" })] },
  { calls: [call("propose_hypothesis", { statement: "a scheduled task locks the account" }), call("propose_hypothesis", { statement: "a password spray" })] },
  { calls: [call("link_evidence", { links: [{ evidence_id: "r1", hypothesis_id: "h1", stance: "supports" }, { evidence_id: "r1", hypothesis_id: "h2", stance: "weakens" }] })] },
  { calls: [call("open_question", { question: "who owns the task?", hypothesis_id: "h1" })] },
  STOP,
  concluded("benign", ["r1"], ["the task's owner is unconfirmed"]),
  { emit: { stance: "uphold", reasons: "the task is named in the finding", missing: [] } },
];

async function dismissed(): Promise<{ state: InProcessState<AgentKinds>; provider: ScriptedProvider }> {
  const spec = specOf();
  const plugin = quiet(pluginFor("investigate"));
  const state = new InProcessState<AgentKinds>();
  const provider = scriptedProvider(DISMISSAL);
  const report = await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));
  expect(report.status).toBe("completed");
  return { state, provider };
}

describe("the hypothesis record", () => {
  it("is kept through the kernel's tools as typed events every reader folds the same way", async () => {
    const { state } = await dismissed();
    const events = await state.read(RUN);
    const record = recordOf(events);
    expect([...record.hypotheses.values()].map((one) => [one.hypothesis_id, one.statement, one.provenance])).toEqual([
      ["h1", "a scheduled task locks the account", "agent"],
      ["h2", "a password spray", "agent"],
    ]);
    expect(record.links).toEqual([
      { evidence_id: "r1", hypothesis_id: "h1", relation: "supports" },
      { evidence_id: "r1", hypothesis_id: "h2", relation: "weakens" },
    ]);
    expect([...record.questions.values()]).toEqual([{ question_id: "q1", question: "who owns the task?", status: "open", hypothesis_id: "h1" }]);
  });

  it("declares the run's gaps on the conclusion: open questions, its own, and every blind call", async () => {
    const { state } = await dismissed();
    const conclusion = (await state.read(RUN)).find((one) => one.kind === "conclusion")?.payload as ConclusionPayload;
    expect(conclusion.declared_gaps).toEqual({
      open_questions: ["who owns the task?"],
      declared: ["the task's owner is unconfirmed"],
      visibility: [],
    });
  });

  it("lets the dismissal stand only because the record links its cited result to a claim", async () => {
    const { state } = await dismissed();
    const review = (await state.read(RUN)).find((one) => one.kind === "review")?.payload as ReviewPayload;
    expect(review).toMatchObject({ stance: "uphold", grounded: true, guarded: { final: "benign" } });
  });

  it("refuses to link a result that was not served, or a claim that is not on the record", async () => {
    const spec = specOf();
    const plugin = quiet(pluginFor("investigate"));
    const state = new InProcessState<AgentKinds>();
    const tools = spec.tools.map((one) => (one.id === "splunk_search" ? tool(one.id, () => ({ ok: false, failure: { kind: "timeout", timeoutMs: 1_000 } })) : tool(one.id)));
    const provider = scriptedProvider([
      { calls: [call("splunk_search", { q: "logons" })] },
      { calls: [call("propose_hypothesis", { statement: "spray" })] },
      { calls: [call("link_evidence", { links: [{ evidence_id: "r1", hypothesis_id: "h1", stance: "supports" }] })] },
      { calls: [call("link_evidence", { links: [{ evidence_id: "r2", hypothesis_id: "h9", stance: "supports" }] })] },
      STOP,
      concluded("malicious", ["r1"]),
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state, tools), optionsOf(spec, plugin));

    const calls = (await state.read(RUN)).filter((one) => one.kind === "call").map((one) => one.payload as CallPayload);
    expect(calls.map((one) => [one.ref, one.tool, one.class])).toEqual([
      ["r1", "splunk_search", "visibility_gap"],
      ["r2", "propose_hypothesis", "served"],
      ["r3", "link_evidence", "defect"],
      ["r4", "link_evidence", "defect"],
    ]);
    expect(calls[2]?.result).toContain("r1 was a visibility gap, not evidence");
    expect(calls[3]?.result).toContain("h9 is not on the record");
    expect(recordOf(await state.read(RUN)).links).toEqual([]);
  });
});

describe("the record costs as few model calls as it can", () => {
  it("proposes a hypothesis by statement in the same call that links evidence to it", async () => {
    const spec = specOf();
    const plugin = quiet(pluginFor("investigate"));
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { calls: [call("get_finding")] },
      // One turn: the next query and the whole record step, run together.
      {
        calls: [
          call("splunk_search", { sql: "select 1" }),
          call("link_evidence", {
            links: [
              { evidence_id: "r1", statement: "a scheduled task", stance: "supports" },
              { evidence_id: "r1", statement: "a password spray", stance: "weakens" },
              { evidence_id: "r1", statement: "a scheduled task", stance: "supports" },
            ],
          }),
        ],
      },
      STOP,
      concluded("malicious", ["r1"]),
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));

    const record = recordOf(await state.read(RUN));
    expect([...record.hypotheses.values()].map((one) => [one.hypothesis_id, one.statement])).toEqual([
      ["h1", "a scheduled task"],
      ["h2", "a password spray"],
    ]);
    expect(record.links.map((one) => [one.hypothesis_id, one.relation])).toEqual([["h1", "supports"], ["h2", "weakens"], ["h1", "supports"]]);
    expect(provider.requests[0]?.messages[0]?.content).toContain("## Keeping the record");
  });

  it("refuses a link that names neither an id nor a statement", async () => {
    const spec = specOf();
    const plugin = quiet(pluginFor("investigate"));
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { calls: [call("get_finding")] },
      { calls: [call("link_evidence", { links: [{ evidence_id: "r1", stance: "supports" }] })] },
      STOP,
      concluded("malicious", ["r1"]),
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));
    const link = (await state.read(RUN)).filter((one) => one.kind === "call").map((one) => one.payload as CallPayload)[1];
    expect(link?.class).toBe("defect");
    expect(link?.result).toContain("names a hypothesis_id, or a statement");
  });
});

describe("the reviewer reads what the answer stands on", () => {
  it("sees every cited record, a bounded window of the rest, and no playbook", async () => {
    const spec = specOf();
    const plugin = quiet(pluginFor("investigate"));
    const state = new InProcessState<AgentKinds>();
    const big = (id: string): RegisteredTool => tool(id, () => ({ ok: true, rows: [{ blob: "x".repeat(7_000) }], rowCount: 1, capped: false, sourceSystem: id }));
    const tools = spec.tools.map((one) => (one.id === "splunk_search" ? big(one.id) : tool(one.id)));
    const provider = scriptedProvider([
      { calls: [call("load_skill", { name: "investigate-case" }), call("get_finding")] },
      { calls: [1, 2, 3, 4, 5].map((n) => call("splunk_search", { sql: `q${n}` })) },
      { calls: [call("link_evidence", { links: [{ evidence_id: "r2", statement: "a scheduled task", stance: "supports" }] })] },
      STOP,
      concluded("benign", ["r2"]),
      { emit: { stance: "uphold", reasons: "named owner", missing: [] } },
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state, tools), optionsOf(spec, plugin));

    const shown = provider.requests.at(-1)?.messages.map((one) => one.content).join("\n") ?? "";
    expect(shown).toContain('<vigil:record id="r2" cited="true">');
    expect(shown).not.toContain('id="r1"');
    // Five 7k results, newest first, until the context window is spent.
    const context = (shown.match(/<vigil:record id="r[3-7]">/g) ?? []).length;
    expect(context).toBeGreaterThan(0);
    expect(context).toBeLessThan(5);
    expect(shown.length).toBeLessThan(30_000);
  });
});

describe("tool results are classified by the kernel", () => {
  it("as served, a visibility gap, or a defect, and only gaps are declared", async () => {
    expect(classify({ ok: true, rows: [], rowCount: 0, capped: false, sourceSystem: "x" })).toBe("served");
    expect(classify({ ok: false, failure: { kind: "timeout", timeoutMs: 1 } })).toBe("visibility_gap");
    expect(classify({ ok: false, failure: { kind: "unavailable", detail: "" } })).toBe("visibility_gap");
    expect(classify({ ok: false, failure: { kind: "backend_error", detail: "" } })).toBe("visibility_gap");
    expect(classify({ ok: false, failure: { kind: "refused", detail: "" } })).toBe("defect");
    expect(classify({ ok: false, failure: { kind: "invalid_args", detail: "" } })).toBe("defect");

    const spec = specOf();
    const plugin = quiet(pluginFor("investigate"));
    const state = new InProcessState<AgentKinds>();
    const tools = spec.tools.map((one) => (one.id === "splunk_search" ? tool(one.id, () => ({ ok: false, failure: { kind: "unavailable", detail: "the index is down" } })) : tool(one.id)));
    const provider = scriptedProvider([
      { calls: [call("splunk_search"), call("no_such_tool")] },
      STOP,
      concluded("inconclusive", []),
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state, tools), optionsOf(spec, plugin));
    const conclusion = (await state.read(RUN)).find((one) => one.kind === "conclusion")?.payload as ConclusionPayload;
    // The ungranted call is the agent's defect and is nobody's blind spot.
    expect(conclusion.declared_gaps?.visibility).toEqual([{ ref: "r1", tool: "splunk_search", failure: "unavailable" }]);
  });
});

describe("a frame per model call", () => {
  it("journals what every call was sent, by hash, before it is sent", async () => {
    const { state, provider } = await dismissed();
    const frames = (await state.read(RUN)).filter((one) => one.kind === "frame").map((one) => one.payload as FramePayload);
    // Five tool turns and the emission; the reviewer's call is on its own ledger.
    expect(frames.map((one) => one.role)).toEqual(["agent", "agent", "agent", "agent", "agent", "agent"]);
    const sent = provider.requests.slice(0, 6).map((request) => inputHash(request.messages, request.tools, request.emit ?? null));
    expect(frames.map((one) => one.input)).toEqual(sent);
    expect(frames[0]).toMatchObject({ call: 1, model_id: "scripted/model", provider_type: "scripted", transcript: [], tail: null, emit: null });
    expect(frames[0]?.skills.map((one) => one.name)).toEqual(["investigate-case"]);
    expect(frames[1]?.transcript).toEqual(["r1"]);
    expect(frames[5]?.tail).not.toBeNull();
    expect(frames[5]?.tools).toEqual([]);
  });

  it("stores each piece of content once, however many calls carry it", async () => {
    const { state } = await dismissed();
    const hashes = (await state.read(RUN)).filter((one) => one.kind === "content").map((one) => (one.payload as { hash: string }).hash);
    expect(new Set(hashes).size).toBe(hashes.length);
  });
});

describe("judges run as child runs", () => {
  it("puts the reviewer on a ledger of its own, linked both ways, with its spend on the parent", async () => {
    const { state } = await dismissed();
    const parent = await state.read(RUN);
    const link = parent.find((one) => one.kind === "child")?.payload as ChildPayload;
    const review = parent.find((one) => one.kind === "review")?.payload as ReviewPayload;
    expect(link).toMatchObject({ child_run_id: childRunId(RUN, "reviewer"), role: "reviewer", key: "reviewer", outcome: "completed" });
    expect(review.child_run_id).toBe(link.child_run_id);

    const child = await state.read(link.child_run_id);
    expect(child.map((one) => one.kind)).toEqual(["run", ...Array(child.length - 4).fill("content"), "frame", "conclusion", "terminal"]);
    expect((child[0]?.payload as RunPayload)).toMatchObject({ run_kind: "subagent", parent: { run_id: RUN, run_kind: "investigate", role: "reviewer" } });
    expect(child.every((one) => one.run_kind === "subagent")).toBe(true);
    // Spend is the budget owner's: the parent carries the reviewer's call.
    expect(child.some((one) => one.kind === "spend")).toBe(false);
    expect(parent.filter((one) => one.kind === "spend")).toHaveLength(7);
  });

  it("reads a child back rather than asking again when the parent resumes after it ended", async () => {
    const { state } = await dismissed();
    const before = (await state.read(childRunId(RUN, "reviewer"))).length;
    const spec = specOf();
    const plugin = quiet(pluginFor("investigate"));
    // Nothing left in the script: a second ask of the reviewer would throw.
    await runAgent(harnessOf(spec, plugin, scriptedProvider([]), state), optionsOf(spec, plugin));
    expect((await state.read(childRunId(RUN, "reviewer"))).length).toBe(before);
  });
});

describe("replay reads the ledger and nothing else", () => {
  it("rebuilds every call's input from the ledger and finds it is what was sent", async () => {
    const { state } = await dismissed();
    const replayed = replayFrames(await state.read(RUN));
    expect(replayed.frames).toHaveLength(6);
    expect(replayed.frames.filter((one) => one.mismatch !== null)).toEqual([]);
    expect(replayed.missing).toEqual([]);

    const child = replayFrames(await state.read(childRunId(RUN, "reviewer")));
    expect(child.reproduced).toBe(1);
  });

  it("says so when the ledger no longer rebuilds what was sent", async () => {
    const { state } = await dismissed();
    const events = (await state.read(RUN)).map((one) =>
      one.kind === "call" && (one.payload as CallPayload).ref === "r1" ? { ...one, payload: { ...(one.payload as CallPayload), result: "something else" } } : one,
    );
    const replayed = replayFrames(events);
    // The first call is untouched; every call that carried r1 after it is not.
    expect(replayed.frames[0]?.mismatch).toBeNull();
    expect(replayed.frames.slice(1).every((one) => one.mismatch !== null)).toBe(true);
  });

  it("reproduces a run that parked on an approval and resumed", async () => {
    const spec = specOf();
    const plugin = quiet(pluginFor("investigate"));
    const state = new InProcessState<AgentKinds>();
    const first = scriptedProvider([{ content: "look", calls: [call("get_finding"), call("case_records", { case_id: "c-1" })] }]);
    const parked = await runAgent(harnessOf(spec, plugin, first, state), optionsOf(spec, plugin));
    expect(parked.status).toBe("waiting_approval");

    const approve: ResolutionPayload = { checkpoint_id: parked.pending?.checkpoint_id as string, actor: "analyst", answer: "approve", text: "", resolved_at: new Date().toISOString() };
    const second = scriptedProvider([{ calls: [call("case_records", { case_id: "c-1" })] }, STOP, concluded("malicious", ["r1", "r2"])]);
    await runAgent(harnessOf(spec, plugin, second, state), optionsOf(spec, plugin, { answers: async () => [approve] }));

    const replayed = replayFrames(await state.read(RUN));
    expect(replayed.frames.length).toBe(4);
    expect(replayed.frames.filter((one) => one.mismatch !== null)).toEqual([]);
  });

  it("hands a learner a trajectory built from the ledger alone", async () => {
    const { state } = await dismissed();
    const trajectory = trajectoryOf(RUN, await state.read(RUN));
    expect(trajectory.faithful).toBe(true);
    expect(trajectory.steps).toHaveLength(6);
    expect(trajectory.steps[0]?.actions.map((one) => [one.ref, one.tool, one.class])).toEqual([["r1", "get_finding", "served"]]);
    expect(trajectory.steps[0]?.input[0]?.role).toBe("system");
    expect(trajectory.steps[5]?.answer).toMatchObject({ disposition: "benign" });
    expect(trajectory.review?.guarded?.final).toBe("benign");
    expect(trajectory.terminal?.outcome).toBe("completed");
    expect(trajectory.children.map((one) => one.role)).toEqual(["reviewer"]);
  });
});

describe("a run that stops early is asked once to keep going", () => {
  const persistent = pluginFor("investigate");
  const ask = (provider: ScriptedProvider): number =>
    provider.requests.filter((request) => request.messages.at(-1)?.role === "user" && request.messages.at(-1)?.content.includes("Your answer will be final")).length;

  it("asks when it stops having looked at little, and goes on when the model does", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { content: "the investigation has just begun", calls: [call("get_finding")] },
      { content: "nothing more to do", calls: [] },
      { calls: [call("splunk_search", { sql: "select * from events" }), call("search_findings", { query: "ws" })] },
      STOP,
      concluded("malicious", ["r1", "r2"]),
    ]);
    await runAgent(harnessOf(spec, persistent, provider, state), optionsOf(spec, persistent));

    expect(ask(provider)).toBe(1);
    // The ask carries what the model said when it stopped.
    expect(provider.requests[2]?.messages.at(-1)?.content).toContain("nothing more to do");
    const calls = (await state.read(RUN)).filter((one) => one.kind === "call").map((one) => (one.payload as CallPayload).tool);
    expect(calls).toEqual(["get_finding", "splunk_search", "search_findings"]);
    // The ask is the asked call's tail, journaled on its frame, so the run still replays.
    const replayed = replayFrames(await state.read(RUN));
    expect(replayed.frames.filter((one) => one.mismatch !== null)).toEqual([]);
    const frames = (await state.read(RUN)).filter((one) => one.kind === "frame").map((one) => one.payload as FramePayload);
    expect(frames[2]?.tail).not.toBeNull();
  });

  it("asks only once, and the second stop stands", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([{ calls: [call("get_finding")] }, STOP, STOP, concluded("inconclusive", [])]);
    const report = await runAgent(harnessOf(spec, persistent, provider, state), optionsOf(spec, persistent));
    expect(report.status).toBe("completed");
    expect(ask(provider)).toBe(1);
  });

  it("does not ask a run that has already looked enough", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { calls: [call("get_finding"), call("splunk_search", { sql: "a" }), call("search_findings"), call("lookup_indicators", { values: ["x"] })] },
      STOP,
      concluded("malicious", ["r1"]),
    ]);
    await runAgent(harnessOf(spec, persistent, provider, state), optionsOf(spec, persistent));
    expect(ask(provider)).toBe(0);
  });

  it("does not count loading a playbook or writing the record as looking", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { calls: [call("load_skill", { name: "investigate-case" }), call("propose_hypothesis", { statement: "x" }), call("open_question", { question: "y" }), call("get_finding")] },
      STOP,
      STOP,
      concluded("inconclusive", []),
    ]);
    await runAgent(harnessOf(spec, persistent, provider, state), optionsOf(spec, persistent));
    expect(ask(provider)).toBe(1);
  });
});

describe("an agent can answer by tool, in the turn it decides", () => {
  const plugin = pluginFor("investigate");
  const answer = (disposition: string, citations: string[]) => ({ summary: `the lockouts are ${disposition}`, disposition, confidence: "medium", citations, gaps: [], next_steps: [] });
  const looked = [call("get_finding"), call("splunk_search", { sql: "a" }), call("search_findings"), call("lookup_indicators", { values: ["x"] })];

  it("ends the run on conclude, with no model call asking for the answer", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([{ calls: looked }, { calls: [call("conclude", answer("malicious", ["r2"]))] }]);
    const report = await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));

    expect(report.status).toBe("completed");
    expect(provider.requests).toHaveLength(2);
    expect(provider.requests.some((request) => request.emit !== undefined)).toBe(false);
    const events = await state.read(RUN);
    const conclusion = events.find((one) => one.kind === "conclusion")?.payload as ConclusionPayload;
    expect(conclusion.value).toMatchObject({ disposition: "malicious", citations: ["r2"] });
    expect(await state.terminal(RUN)).toMatchObject({ outcome: "completed", summary: "the lockouts are malicious" });
    expect(replayFrames(events).frames.filter((one) => one.mismatch !== null)).toEqual([]);
  });

  it("refuses an answer that does not match its shape, and takes the corrected one", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { calls: looked },
      { calls: [call("conclude", { summary: "x", disposition: "probably bad" })] },
      { calls: [call("conclude", answer("suspicious", []))] },
    ]);
    await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));
    const concluded = (await state.read(RUN)).filter((one) => one.kind === "call" && (one.payload as CallPayload).tool === "conclude").map((one) => one.payload as CallPayload);
    expect(concluded.map((one) => one.failure)).toEqual(["invalid_args", null]);
    expect(((await state.read(RUN)).find((one) => one.kind === "conclusion")?.payload as ConclusionPayload).value["disposition"]).toBe("suspicious");
  });

  it("refuses a first answer over too little looking, once, and the second stands", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([
      { calls: [call("get_finding")] },
      { calls: [call("conclude", answer("inconclusive", []))] },
      { calls: [call("conclude", answer("inconclusive", []))] },
    ]);
    const report = await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));
    expect(report.status).toBe("completed");
    const concluded = (await state.read(RUN)).filter((one) => one.kind === "call" && (one.payload as CallPayload).tool === "conclude").map((one) => one.payload as CallPayload);
    expect(concluded.map((one) => one.failure)).toEqual(["refused", null]);
    expect(concluded[0]?.result).toContain("call conclude again");
  });

  it("still asks for the answer when the agent stops without calling conclude", async () => {
    const spec = specOf();
    const state = new InProcessState<AgentKinds>();
    const provider = scriptedProvider([{ calls: looked }, STOP, concluded("malicious", ["r1"])]);
    const report = await runAgent(harnessOf(spec, plugin, provider, state), optionsOf(spec, plugin));
    expect(report.status).toBe("completed");
    expect(provider.requests.at(-1)?.emit).toBeDefined();
  });
});
