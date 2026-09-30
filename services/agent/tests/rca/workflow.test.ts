import { describe, expect, it } from "vitest";
import { fileURLToPath } from "node:url";
import type { NewEvent } from "../../contracts/events.js";
import type { RegisteredTool, ToolResult } from "../../contracts/tool.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import { registryOf } from "../../core/registry.js";
import type { ToolDispatch } from "../../core/seams.js";
import { assembleSpec, loadArch, parseConfig, parsePlaybook, type RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import { RCA_ACTIONS, RCA_PERMIT, type RcaKinds, type SegmentPayload } from "../../workflows/rca/vocabulary.js";
import { runRootCause } from "../../workflows/rca/workflow.js";
import { scriptedProvider, type ScriptedTurn } from "../support/scripted-provider.js";

const RUN = "7d3c2d3e-0000-4000-8000-000000001284";
const T0 = Date.parse("2026-08-13T10:00:00Z") / 1000;
const iso = (epoch: number) => new Date(epoch * 1000).toISOString();

// A phishing mail delivers a document, Word runs it, the process it spawns beacons.
const MAIL = { _time: iso(T0), sourcetype: "mail", msgid: "m-77", sender: "evil@x.io", attachment: "inv.docm" };
const PROC = { _time: iso(T0 + 60), sourcetype: "proc", event_id: "proc-evt-1", host: "PC1", process: "winword.exe", file: "inv.docm", pid: "4242" };
const C2 = { _time: iso(T0 + 120), sourcetype: "net", conn_id: "conn-9", host: "PC1", pid: "4242", dest: "45.1.2.3" };

interface Store {
  // Events carrying a value before the cause: what the provenance check asks.
  prior?: number;
  // Events mentioning an origin's who before it, and at all.
  earlier?: number;
  total?: number;
}

// Splunk as splunk_execute answers it, keyed on what the SPL asks.
function splunk(store: Store = {}): { dispatch: ToolDispatch; asked: string[] } {
  const asked: string[] = [];
  const answer = (spl: string): unknown[] => {
    if (spl.startsWith("| tstats")) {
      return ["mail", "proc", "net"].map((sourcetype) => ({ index: "main", sourcetype, count: "10", first: String(T0 - 86_400), last: String(T0 + 3_600) }));
    }
    if (spl.includes("fieldsummary")) return ["msgid", "sender", "attachment", "event_id", "host", "process", "file", "pid", "conn_id", "dest"].map((field) => ({ field }));
    if (spl.includes("| regex _raw=")) return [{ count: String(store.prior ?? 0) }];
    if (spl.includes("stats count by sourcetype")) return [{ sourcetype: "proc", count: "1" }, { sourcetype: "net", count: "1" }];
    if (spl.endsWith("| stats count")) return [{ count: String(/latest=\d+/.test(spl) ? (store.earlier ?? 0) : (store.total ?? 1)) }];
    if (spl.includes("sourcetype=net")) return [C2];
    if (spl.includes("sourcetype=proc")) return [PROC];
    if (spl.includes("sourcetype=mail")) return [MAIL];
    return [];
  };
  return {
    asked,
    dispatch: {
      invoke: async (tool: RegisteredTool, args: Record<string, unknown>): Promise<ToolResult> => {
        if (tool.local) return tool.invoke(args);
        const spl = String(args["spl_query"]);
        asked.push(spl);
        return { ok: true, rows: [{ success: true, results: answer(spl) }], rowCount: 1, capped: false, sourceSystem: "splunk" };
      },
    },
  };
}

const TELEMETRY = (id: string) => `
  - id: ${id}
    kind: remote
    provides: telemetry_search
    description: Execute SPL query
    parameters: { type: object }
    max_rows: 500
    timeout_ms: 120000`;

function specOf({ permit = "auto", tool = "splunk-selfhosted_splunk_execute", segments = 3 } = {}): RunSpec {
  const config = `
model: scripted/model
tools:${TELEMETRY(tool)}
thresholds: { max_iterations: ${segments} }
runtime: { max_turns: 12 }
checkpoints: { ${RCA_PERMIT}: ${permit} }
`;
  return assembleSpec({
    arch: loadArch(fileURLToPath(new URL("../../arch/rootcause.yaml", import.meta.url)), RCA_ACTIONS),
    playbook: parsePlaybook("---\nname: root-cause-analysis\n---\nTrace the confirmed compromise back to its origin."),
    config: parseConfig(config, { config: ["checkpoints"] }),
    prompt: "**Additional Context:** PC1 beacons to 45.1.2.3 from pid 4242.",
  });
}

function harnessOf(script: readonly ScriptedTurn[], dispatch: ToolDispatch, state = new InProcessState<RcaKinds>()): Harness<RcaKinds> {
  return {
    provider: scriptedProvider(script),
    registry: registryOf([], {}),
    dispatch,
    budget: budgetOf({ max_calls: 100, max_cost_usd: 100, max_wall_ms: 600_000, max_park_ms: 604_800_000 }, unmeteredQuota),
    memory: nullMemory,
    state,
  };
}

const query = (spl: string): ScriptedTurn => ({ calls: [{ tool: "splunk_query", args: JSON.stringify({ spl }) }] });
const write = (finding: Record<string, string>): ScriptedTurn => ({ calls: [{ tool: "findings", args: JSON.stringify({ op: "write", ...finding }) }] });
const STOP: ScriptedTurn = { calls: [] };
const report = (text: string): ScriptedTurn => ({ emit: { action: "FINISH", report: text } });

const F1 = { what: "connected to 45.1.2.3", who: "PC1", session: "4242", when: C2._time, evidence: "conn-9" };
const F2 = { what: "winword.exe opened inv.docm", who: "PC1", session: "4242", when: PROC._time, evidence: "proc-evt-1", why: "F3", link: "inv.docm" };
const F3 = { what: "mailed inv.docm", who: "evil@x.io", session: "none", when: MAIL._time, evidence: "m-77", why: "origin" };
const REPORT = "evil@x.io mailed inv.docm; Word on PC1 opened it and pid 4242 beaconed to 45.1.2.3. Block the sender.";

const CHAIN: ScriptedTurn[] = [
  query("index=main sourcetype=net dest=45.1.2.3"),
  write({ ...F1, why: "F2", link: "4242" }),
  query("index=main sourcetype=proc pid=4242"),
  write(F2),
  query("index=main sourcetype=mail attachment=inv.docm"),
  write(F3),
  STOP,
  report(REPORT),
];

async function kinds(state: InProcessState<RcaKinds>): Promise<string[]> {
  return (await state.read(RUN)).map((event) => event.kind);
}

describe("a root-cause run", () => {
  it("traces a proven chain to its origin and reports it", async () => {
    const state = new InProcessState<RcaKinds>();
    const done = await runRootCause(harnessOf(CHAIN, splunk().dispatch, state), { run_id: RUN, run_kind: "root_cause", spec: specOf() });

    expect(done.status).toBe("completed");
    expect(await state.terminal(RUN)).toMatchObject({ outcome: "completed", summary: REPORT });
    const notebook = (await state.read(RUN)).filter((event) => event.kind === "notebook").at(-1)?.payload as RcaKinds["notebook"];
    expect(notebook.list.map((one) => one.id)).toEqual(["F1", "F2", "F3"]);
    expect(await kinds(state)).toContain("survey");
  });

  it("sends the report back while a why is open, and takes it once the cause is linked", async () => {
    const state = new InProcessState<RcaKinds>();
    const script: ScriptedTurn[] = [
      query("index=main sourcetype=net dest=45.1.2.3"),
      write({ ...F1, why: "unknown" }),
      STOP,
      report("PC1 beaconed to 45.1.2.3; how it began is unknown."),
      ...CHAIN.slice(2, 6),
      write({ id: "F1", why: "F2", link: "4242" }),
      STOP,
      report(REPORT),
    ];
    const done = await runRootCause(harnessOf(script, splunk().dispatch, state), { run_id: RUN, run_kind: "root_cause", spec: specOf() });

    expect(done.status).toBe("completed");
    const segments = (await state.read(RUN)).filter((event) => event.kind === "segment").map((event) => event.payload as SegmentPayload);
    expect(segments).toHaveLength(2);
    expect(segments[0]?.refused).toMatch(/1 why still open: F1/);
    expect(segments[1]?.refused).toBeNull();
    expect((await state.terminal(RUN))?.summary).toBe(REPORT);
  });

  it("does not take a link that already existed before its cause", async () => {
    const state = new InProcessState<RcaKinds>();
    // inv.docm was in 30 events before the mail: a value that already existed proves
    // nothing about which event produced the next. pid 4242 still holds, as taint --
    // it sits in a process field of both rows and the beacon came after.
    const script: ScriptedTurn[] = [...CHAIN, STOP, report(REPORT)];
    await runRootCause(harnessOf(script, splunk({ prior: 30 }).dispatch, state), { run_id: RUN, run_kind: "root_cause", spec: specOf({ segments: 2 }) });

    const segments = (await state.read(RUN)).filter((event) => event.kind === "segment").map((event) => event.payload as SegmentPayload);
    expect(segments[0]?.refused).toMatch(/Open: F2 \(link "inv.docm" is in 30 events before F3's event, so F3 did not produce it/);
    // So the sender is not reached by a proven chain, and may not be named.
    expect(segments[0]?.refused).toMatch(/^refused: the report names "evil@x.io"/);
    // The second stretch is the last, so the gate has no budget left to refuse with.
    expect(segments[1]?.refused).toBeNull();
    expect((await state.terminal(RUN))?.reason).toMatch(/reported after 2 stretch/);
  });

  it("refuses an origin whose who appears earlier in the logs", async () => {
    const state = new InProcessState<RcaKinds>();
    const script: ScriptedTurn[] = [...CHAIN.slice(0, 7), report(REPORT), STOP, report(REPORT)];
    await runRootCause(harnessOf(script, splunk({ earlier: 3 }).dispatch, state), { run_id: RUN, run_kind: "root_cause", spec: specOf({ segments: 2 }) });

    const segments = (await state.read(RUN)).filter((event) => event.kind === "segment").map((event) => event.payload as SegmentPayload);
    expect(segments[0]?.refused).toMatch(/not an origin: "evil@x.io" is in 3 earlier events/);
  });

  it("replaces an actor the proven chain does not reach", async () => {
    const state = new InProcessState<RcaKinds>();
    const decoy = { what: "logged in", who: "admin", session: "none", when: iso(T0 - 30), evidence: "decoy-1", why: "unknown" };
    const dispatch = splunk();
    const withDecoy: ToolDispatch = {
      invoke: async (tool, args) =>
        !tool.local && String(args["spl_query"]).includes("sourcetype=auth")
          ? { ok: true, rows: [{ success: true, results: [{ _time: decoy.when, sourcetype: "auth", event_id: "decoy-1", user: "admin" }] }], rowCount: 1, capped: false, sourceSystem: "splunk" }
          : dispatch.dispatch.invoke(tool, args),
    };
    const script: ScriptedTurn[] = [
      ...CHAIN.slice(0, 6),
      query("index=main sourcetype=auth user=admin"),
      write(decoy),
      STOP,
      report("admin was the attacker. " + REPORT),
    ];
    await runRootCause(harnessOf(script, withDecoy, state), { run_id: RUN, run_kind: "root_cause", spec: specOf({ segments: 1 }) });

    expect((await state.terminal(RUN))?.summary).toMatch(/^\[unlinked actor\] was the attacker\./);
  });

  it("resumes from the ledger: a stretch already judged is not run again", async () => {
    const state = new InProcessState<RcaKinds>();
    const script: ScriptedTurn[] = [query("index=main sourcetype=net dest=45.1.2.3"), write({ ...F1, why: "unknown" }), STOP, report("unfinished")];
    const options = { run_id: RUN, run_kind: "root_cause" as const, spec: specOf() };
    // The first stretch is refused; the script then runs out, which fails the run's
    // second stretch -- stand in for a worker that died, by dropping that terminal.
    await runRootCause(harnessOf(script, splunk().dispatch, state), options);
    const kept = (await state.read(RUN)).filter((one) => one.kind !== "terminal");
    const resumed = new InProcessState<RcaKinds>();
    await resumed.append(RUN, kept.map(({ seq: _seq, ts: _ts, schema_version: _v, ...rest }) => rest) as NewEvent<RcaKinds>[]);

    const done = await runRootCause(harnessOf([...CHAIN.slice(2, 6), write({ id: "F1", why: "F2", link: "4242" }), STOP, report(REPORT)], splunk().dispatch, resumed), options);

    expect(done.status).toBe("completed");
    // The notebook came back: F1 was updated, not written again.
    const notebook = (await resumed.read(RUN)).filter((one) => one.kind === "notebook").at(-1)?.payload as RcaKinds["notebook"];
    expect(notebook.list.map((one) => one.id)).toEqual(["F1", "F2", "F3"]);
    const segments = (await resumed.read(RUN)).filter((one) => one.kind === "segment").map((one) => (one.payload as SegmentPayload).segment);
    expect(segments).toEqual([1, 2]);
  });

  it("says up front when the log search is not Splunk, before any model call", async () => {
    const state = new InProcessState<RcaKinds>();
    const done = await runRootCause(harnessOf([], splunk().dispatch, state), {
      run_id: RUN,
      run_kind: "root_cause",
      spec: specOf({ tool: "elastic_elastic_search_logs" }),
    });

    expect(done.status).toBe("failed");
    expect(done.reason).toMatch(/needs Splunk \(splunk_execute\); this deployment searches logs with elastic_elastic_search_logs/);
    expect(await kinds(state)).toEqual(["run", "terminal"]);
  });
});

describe("the permit to trace", () => {
  const resolve = async (state: InProcessState<RcaKinds>, answer: "approve" | "reject") => {
    const raised = (await state.read(RUN)).find((event) => event.kind === "checkpoint");
    const checkpoint_id = (raised?.payload as { checkpoint_id: string }).checkpoint_id;
    await state.append(RUN, [
      { run_id: RUN, run_kind: "root_cause", kind: "resolution", payload: { checkpoint_id, actor: "ana", answer, text: answer === "reject" ? "not ours" : "", resolved_at: iso(T0) } } as NewEvent<RcaKinds>,
    ]);
  };

  it("parks before spending anything, then traces once permitted", async () => {
    const state = new InProcessState<RcaKinds>();
    const announced: string[] = [];
    const options = { run_id: RUN, run_kind: "root_cause" as const, spec: specOf({ permit: "ask" }), announce: async (_run: string, _kind: string, payload: { checkpoint_class: string; question: string }) => void announced.push(`${payload.checkpoint_class}: ${payload.question}`) };
    const store = splunk();
    const parked = await runRootCause(harnessOf([], store.dispatch, state), options);

    expect(parked.status).toBe("waiting_approval");
    expect(announced[0]).toMatch(/^rca_permit: Permit a root-cause trace of this finding back to how it began\? PC1 beacons/);
    expect(announced[0]).not.toMatch(/hypothes/i);
    expect(store.asked).toHaveLength(0);
    expect(await kinds(state)).toEqual(["run", "checkpoint"]);

    await resolve(state, "approve");
    const done = await runRootCause(harnessOf(CHAIN, store.dispatch, state), options);
    expect(done.status).toBe("completed");
  });

  it("ends aborted when the trace is not permitted", async () => {
    const state = new InProcessState<RcaKinds>();
    const options = { run_id: RUN, run_kind: "root_cause" as const, spec: specOf({ permit: "ask" }) };
    await runRootCause(harnessOf([], splunk().dispatch, state), options);
    await resolve(state, "reject");
    const done = await runRootCause(harnessOf([], splunk().dispatch, state), options);

    expect(done.status).toBe("aborted");
    expect(done.reason).toBe("ana did not permit the trace: not ours");
  });
});
