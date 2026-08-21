import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { archFor } from "../../arch/registry.js";
import { defineTool, type RegisteredTool } from "../../contracts/tool.js";
import type { RunKind } from "../../contracts/events.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import { localDispatch } from "../../core/dispatch.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import { registryOf } from "../../core/registry.js";
import { buildSpec, type RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import type { Answers } from "../../core/answers.js";
import { grantsOf, runLead, type LeadKinds, type LeadOptions } from "../../workflows/lead/workflow.js";
import { isLead, respondingProvider } from "../support/responding-provider.js";
import { scriptedProvider, type ScriptedTurn } from "../support/scripted-provider.js";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const RUN = "7d3c2d3e-0000-4000-8000-000000000624";

// Stands in for whatever the arch named, so a test of the routing is a test of
// the routing: the ids come from the config, never from this file.
function stub(id: string): RegisteredTool {
  return defineTool(
    {
      id,
      description: `stand-in for ${id}`,
      parameters: { type: "object", additionalProperties: false, properties: {} },
      execute: async () => ({ ok: true, rows: [{ id }], rowCount: 1, capped: false, sourceSystem: id }),
    },
    { maxRows: 10, timeoutMs: 1_000 },
  );
}

function specFor(kind: RunKind, playbook: string, config: string): RunSpec {
  const entry = archFor(kind);
  return buildSpec({ arch: entry.arch, playbook: join(FIXTURES, playbook), config: join(FIXTURES, config) }, entry.actions);
}

function harnessOf(spec: RunSpec, script: readonly ScriptedTurn[], state: InProcessState<LeadKinds>): Harness<LeadKinds> {
  const grants = grantsOf(spec);
  const tools = [...new Set(Object.values(grants).flat())].map(stub);
  return {
    provider: scriptedProvider(script),
    registry: registryOf(tools, grants),
    dispatch: localDispatch,
    budget: budgetOf(spec.budgets, unmeteredQuota),
    memory: nullMemory,
    state,
  };
}

function options(kind: RunKind, spec: RunSpec, answers?: Answers): LeadOptions {
  const entry = archFor(kind);
  const from = answers === undefined ? {} : { answers };
  return { run_id: RUN, run_kind: kind, spec, actions: entry.actions, halts: entry.halts, ...from };
}

const STOP: ScriptedTurn = { calls: [] };

const SWARM: ScriptedTurn[] = [
  STOP,
  {
    emit: {
      action: "INVESTIGATE",
      rationale: "characterise the overnight traffic before judging it",
      evidence_citations: [],
      worker_agent_id: "network_analyst",
      query_intent: "periodicity of outbound flows from the finance segment",
    },
  },
  STOP,
  {
    emit: {
      results: [
        {
          source_system: "network",
          summary: "every 300s +/- 4s over 6 hours, 412 connections to 45.77.53.176",
          salience: "anomalous",
          why_notable: "the jitter is too low for a human or a poller",
          payload: JSON.stringify({ interval_s: 300, connections: 412 }),
        },
      ],
    },
  },
  STOP,
  { emit: { action: "CONCLUDE", rationale: "the beaconing is characterised and cited", evidence_citations: [] } },
];

const SINGLE: ScriptedTurn[] = [
  STOP,
  { emit: { action: "EXAMINE", rationale: "the lockouts cluster inside one hour", citations: [] } },
  STOP,
  { emit: { action: "CONCLUDE", rationale: "a scheduled task holding a stale password", citations: [] } },
];

describe("an arch drives the loop", () => {
  it("runs the fan-out arch to completion and dispatches the worker it named", async () => {
    const spec = specFor("hunt", "hunt.playbook.yaml", "hunt.config.yaml");
    const state = new InProcessState<LeadKinds>();
    const report = await runLead(harnessOf(spec, SWARM, state), options("hunt", spec));

    expect(report.status).toBe("completed");
    expect(report.iterations).toBe(2);
    expect(report.dispatched).toBe(1);

    const events = await state.read(RUN);
    expect(events.map((event) => event.kind)).toEqual([
      "run", "spend", "spend", "decision", "spend", "spend", "dispatch", "finding", "spend", "spend", "decision", "terminal",
    ]);
    expect(events.find((event) => event.kind === "dispatch")?.payload).toMatchObject({
      agent_id: "network_analyst",
      status: "complete",
    });
  });

  // The other dispatch mode, on the same loop: no roster, no fan-out, no critic.
  it("runs the single-lead arch to completion with nothing to dispatch to", async () => {
    const spec = specFor("investigate", "case.playbook.yaml", "case.config.yaml");
    const state = new InProcessState<LeadKinds>();
    const report = await runLead(harnessOf(spec, SINGLE, state), options("investigate", spec));

    expect(report.status).toBe("completed");
    expect(report.dispatched).toBe(0);
    expect(await state.terminal(RUN)).toEqual({
      outcome: "completed",
      reason: "a scheduled task holding a stale password",
    });
  });

  it("journals the arch the run started under", async () => {
    const spec = specFor("hunt", "hunt.playbook.yaml", "hunt.config.yaml");
    const state = new InProcessState<LeadKinds>();
    await runLead(harnessOf(spec, SWARM, state), options("hunt", spec));

    const [opened] = await state.read(RUN);
    expect(opened?.payload).toMatchObject({ spec: { arch: "threathunt", dispatch: { max_workers: 4 } } });
  });

  it("parks on a gated call and comes back when the answer arrives", async () => {
    const spec = specFor("investigate", "case.playbook.yaml", "case.config.yaml");
    const state = new InProcessState<LeadKinds>();
    const gated: ScriptedTurn = { calls: [{ tool: "case_records", args: "{}" }] };

    // case.config.yaml gates case_records, so the first call parks the run.
    const parked = await runLead(harnessOf(spec, [gated], state), options("investigate", spec));
    expect(parked.status).toBe("waiting_approval");
    const checkpoint = parked.pending?.checkpoint_id as string;
    expect(checkpoint).toBeTypeOf("string");

    // Resumed with nobody to ask: still parked, because an unanswered gate is not
    // an approval and a resume must not be one either.
    const again = await runLead(harnessOf(spec, [gated], state), options("investigate", spec));
    expect(again.status).toBe("waiting_approval");

    const answered: Answers = async () => [
      { checkpoint_id: checkpoint, actor: "analyst", answer: "approve", text: "", resolved_at: "2026-08-12T00:01:00Z" },
    ];
    const done = await runLead(harnessOf(spec, SINGLE, state), options("investigate", spec, answered));

    expect(done.status).toBe("completed");
    const resolutions = (await state.read(RUN)).filter((event) => event.kind === "resolution");
    expect(resolutions).toHaveLength(1);
  });

  it("declines the call and carries on when the answer was a rejection", async () => {
    const spec = specFor("investigate", "case.playbook.yaml", "case.config.yaml");
    const state = new InProcessState<LeadKinds>();
    const gated: ScriptedTurn = { calls: [{ tool: "case_records", args: "{}" }] };

    const parked = await runLead(harnessOf(spec, [gated], state), options("investigate", spec));
    const rejected: Answers = async () => [
      {
        checkpoint_id: parked.pending?.checkpoint_id as string,
        actor: "analyst",
        answer: "reject",
        text: "not without a change window",
        resolved_at: "2026-08-12T00:01:00Z",
      },
    ];

    // A rejection resumes the run rather than ending it: the lead is told the
    // call was refused and decides again over what it does have.
    const done = await runLead(harnessOf(spec, SINGLE, state), options("investigate", spec, rejected));
    expect(done.status).toBe("completed");
  });

  // A swarm assigns every peer, so it is where mode actually shows.
  function swarmSpec(mode: "serial" | "parallel"): RunSpec {
    const spec = specFor("hunt", "hunt.playbook.yaml", "hunt.config.yaml");
    return { ...spec, dispatch: { ...spec.dispatch, topology: "swarm", mode, max_workers: 3 } };
  }

  // One round: a swarm assigns its peers before the halting action is read, so
  // concluding immediately still dispatches everyone exactly once.
  function peers(mode: "serial" | "parallel") {
    const spec = swarmSpec(mode);
    const provider = respondingProvider({
      emit: (schema) =>
        isLead(schema)
          ? { action: "CONCLUDE", rationale: "characterised", evidence_citations: [] }
          : { results: [] },
    });
    const state = new InProcessState<LeadKinds>();
    const harness = { ...harnessOf(spec, [], state), provider };
    return { spec, state, provider, harness };
  }

  it("runs a parallel round together", async () => {
    const { spec, provider, harness } = peers("parallel");
    const report = await runLead(harness, options("hunt", spec));

    expect(report.dispatched).toBe(3);
    // The point of the mode: more than one turn was in flight at once.
    expect(provider.peak()).toBeGreaterThan(1);
  });

  it("runs a serial round one at a time, whatever the topology assigned", async () => {
    const { spec, provider, harness } = peers("serial");
    const report = await runLead(harness, options("hunt", spec));

    expect(report.dispatched).toBe(3);
    expect(provider.peak()).toBe(1);
  });

  it("keeps every dispatch and finding a parallel round produced", async () => {
    const { spec, state, harness } = peers("parallel");
    await runLead(harness, options("hunt", spec));

    // The failure this guards is silent: a round that lost two findings to a
    // collision still reads as a round that ran three.
    const events = await state.read(RUN);
    expect(events.filter((event) => event.kind === "dispatch")).toHaveLength(3);
    expect(events.filter((event) => event.kind === "finding")).toHaveLength(3);
  });

  it("writes a parallel round in one contiguous stretch", async () => {
    const { spec, state, harness } = peers("parallel");
    await runLead(harness, options("hunt", spec));

    const seqs = (await state.read(RUN)).map((event) => event.seq);
    expect(seqs).toEqual(seqs.map((_, at) => at));
  });

  // A role gets what its arch declared and nothing else -- either named outright
  // or asked for as a capability the config says what provides.
  it("grants each role the tools its arch declares or asked for by capability", () => {
    expect(grantsOf(specFor("hunt", "hunt.playbook.yaml", "hunt.config.yaml"))).toEqual({
      lead: ["expand"],
      critic: [],
      threat_hunter: ["search_findings", "nearest_neighbors", "splunk_search"],
      network_analyst: ["splunk_search", "search_findings"],
      threat_intel: ["lookup_indicators"],
    });
    expect(grantsOf(specFor("investigate", "case.playbook.yaml", "case.config.yaml"))).toEqual({
      lead: ["case_records"],
    });
  });
});
