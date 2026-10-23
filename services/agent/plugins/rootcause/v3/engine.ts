import type { AgentEvent, CallPayload, DirectivePayload, NewEvent, RunOutcome, TerminalPayload } from "../../../contracts/events.js";
import type { RegisteredTool } from "../../../contracts/tool.js";
import type { AgentKinds, StopSource } from "../../../core/agent.js";
import { GatewayExhausted } from "../../../core/limiter.js";
import type { Harness } from "../../../core/loop.js";
import type { RunSpec } from "../../../core/spec.js";
import { toolsFrom } from "../../../tools/remote.js";
import { loadPlugin } from "../../index.js";
import { defaultConfig, type AgentConfig } from "./config.js";
import { frameAlert, Unframed } from "./frame.js";
import { createGraph } from "./graph.js";
import { ENGINE, engineOf, fold, type SetupPayload, type V3Kinds } from "./ledger.js";
import { KernelModelClient, KernelTurns, Refused } from "./model/client.js";
import { run, seedFromAlert } from "./orchestrator.js";
import { renderReport } from "./report.js";
import { scope } from "./scope.js";
import { neverRan, Registry, type Journal } from "./tools/registry.js";
import { sourceFor, UnsupportedSource, type Invoke, type SourceAdapter } from "./tools/source.js";
import { splunkAdapter } from "./tools/splunk-source.js";
import { MemoryTrace } from "./trace.js";
import type { Deps, ExitReason } from "./types.js";

// The v3 loop as a worker run: it opens and journals its own ledger, every move one kernel turn billed to the
// run's pool, searches through the run's bound telemetry tool, and resumes from what it journaled. The kernel
// does not drive it; the worker hands it the harness.

const ADAPTERS: readonly SourceAdapter[] = [splunkAdapter];
const DAY_MS = 86_400_000;
// How often an operator's stop is looked for: a turn can sit on one call for minutes.
const STOP_POLL_MS = 1_000;
// What a call event keeps of a result the code asked for; the trace holds what the model was shown.
const CALL_RESULT_CHARS = 4_000;

export interface V3Options {
  run_id: string;
  spec: RunSpec;
  started_by?: string;
  principal?: string;
  // The lease: aborted when this worker may no longer write the run.
  signal?: AbortSignal;
  stop?: StopSource;
  adapters?: readonly SourceAdapter[];
  log?: (line: string) => void;
}

export interface V3Report {
  // lost: the lease went while the run was going, so it ends with no terminal and resumes.
  status: RunOutcome | "lost";
  reason: string;
}

type State3 = Harness<V3Kinds>["state"];

class Stopped extends Error {
  constructor(readonly directive: DirectivePayload) {
    super("an operator stopped the run");
  }
}

// The run's settings: the loop's defaults under the spec's dollar ceiling. The spec's model serves every role,
// and the pool holds calls and wall clock; v3's wrap-up and closing turns key off maxUsd.
export const configFor = (spec: RunSpec): AgentConfig => ({ ...defaultConfig, maxUsd: spec.budgets.max_cost_usd });

export async function runRootCauseV3(harness: Harness<V3Kinds>, options: V3Options): Promise<V3Report> {
  const { run_id, spec } = options;
  const state = harness.state;
  const log = options.log ?? (() => {});
  const event = <K extends string>(kind: K, payload: unknown) => ({ run_id, run_kind: "root_cause", kind, payload }) as unknown as NewEvent<V3Kinds>;

  const prior = await state.read(run_id);
  if (prior.length === 0) {
    await state.append(run_id, [
      event("run", {
        run_kind: "root_cause",
        spec: { ...spec, sections: { ...spec.sections, engine: ENGINE } },
        budgets: harness.budget.limits,
        seed: run_id,
        tenant_id: null,
        started_by: options.started_by ?? "worker",
        ...(options.principal === undefined ? {} : { principal: options.principal }),
      }),
    ]);
  }

  // A ledger the kernel plugin opened is not this engine's to continue: v3 would write its events onto it.
  if (prior.length > 0 && engineOf(prior) !== ENGINE) {
    const reason = "this run was opened by the kernel plugin's root-cause loop and cannot continue on the v3 engine";
    if ((await state.terminal(run_id)) === null) await state.append(run_id, [event("terminal", { outcome: "failed", reason })]);
    return { status: "failed", reason };
  }

  const halt = new AbortController();
  if (options.signal !== undefined) {
    if (options.signal.aborted) return { status: "lost", reason: String(options.signal.reason ?? "the lease was lost") };
    options.signal.addEventListener("abort", () => halt.abort(options.signal!.reason), { once: true });
  }
  const polling = options.stop === undefined ? undefined : setInterval(() => {
    void options.stop!().then((directive) => {
      if (directive !== null && !halt.signal.aborted) halt.abort(new Stopped(directive));
    }, () => {});
  }, STOP_POLL_MS);

  try {
    return await drive(harness, options, prior as AgentEvent<V3Kinds>[], halt.signal, event, log);
  } finally {
    clearInterval(polling);
  }
}

async function drive(
  harness: Harness<V3Kinds>,
  options: V3Options,
  prior: readonly AgentEvent<V3Kinds>[],
  signal: AbortSignal,
  event: (kind: string, payload: unknown) => NewEvent<V3Kinds>,
  log: (line: string) => void,
): Promise<V3Report> {
  const { run_id, spec } = options;
  const state = harness.state;
  const end = async (outcome: RunOutcome, reason: string, summary?: string): Promise<V3Report> => {
    // Something outside the run may have ended it first; its terminal stands.
    if ((await state.terminal(run_id)) === null) {
      const payload: TerminalPayload = { outcome, reason, ...(summary === undefined ? {} : { summary }) };
      await state.append(run_id, [event("terminal", payload)]);
    }
    return { status: outcome, reason };
  };
  // The pool refusing another call, or the gateway giving out, spends the run's allowance rather than failing it.
  const spentOut = (error: unknown): error is Refused | GatewayExhausted => error instanceof Refused || error instanceof GatewayExhausted;

  let bound: { adapter: SourceAdapter; tool: RegisteredTool };
  const tools = toolsFrom(spec.tools);
  try {
    const picked = sourceFor(options.adapters ?? ADAPTERS, tools.map((t) => t.id));
    bound = { adapter: picked.adapter, tool: tools.find((t) => t.id === picked.tool)! };
  } catch (error) {
    if (error instanceof UnsupportedSource) return end("failed", error.message);
    throw error;
  }

  // The source's own tool, through the agent's dispatch: bounded and attributed like any other call.
  const invoke: Invoke = (args) => harness.dispatch.invoke(bound.tool, args, signal);
  const turns = new KernelTurns(harness as unknown as Harness<AgentKinds>, {
    run_id, run_kind: "root_cause", spec, plugin: loadPlugin("rootcause").manifest, tools, signal,
    ...(options.started_by === undefined ? {} : { started_by: options.started_by }),
  });

  const folded = fold(prior);
  // The run had finished and only its terminal was lost: write it, run nothing more.
  if (folded.exit !== null && folded.report !== null) return end(folded.exit === "budget-exhausted" ? "budget_exhausted" : "completed", `the trace ended: ${folded.exit}`, conclusionOf(folded.report));
  let turnNow = Math.max(0, folded.lastTurn + 1);
  // A search the code makes itself, journaled as the kernel journals a model's: its ref follows the ledger's calls.
  const journal: Journal = async (call) => {
    const shown = call.result.ok ? call.result.data ?? "" : call.result.error ?? "";
    const payload: CallPayload = {
      ref: call.toolCallId,
      turn: turnNow,
      call_id: call.toolCallId,
      tool: call.tool,
      arguments: JSON.stringify(call.input),
      result: shown.length > CALL_RESULT_CHARS ? `${shown.slice(0, CALL_RESULT_CHARS)}…` : shown,
      ok: call.result.ok,
      failure: call.result.ok ? null : "backend_error",
      class: call.result.ok ? "served" : /^timed out/.test(shown) ? "visibility_gap" : "defect",
      role: "code",
      duration_ms: call.ms,
    };
    await state.append(run_id, [event("call", payload)]);
  };
  const registry = new Registry(prior.filter((e) => e.kind === "call").length, journal);

  let setup: SetupPayload;
  try {
    setup = folded.setup ?? (await open(options, bound.adapter, invoke, turns));
  } catch (error) {
    if (error instanceof Unframed) return end("failed", error.message);
    if (spentOut(error)) return end("budget_exhausted", error.message);
    if (signal.aborted) return halted(signal, end, state, run_id, event);
    throw error;
  }

  const { cfg, alert, index, range } = setup;
  const source = bound.adapter.open(invoke, { index, range });
  registry.register(source.evidenceTool());
  // The calls a resumed run already made stay citable, and a new one never reuses a ref.
  for (const call of prior.filter((e) => e.kind === "call").map((e) => e.payload as CallPayload)) registry.note(call.ref, argsOf(call.arguments), call.ok ? call.result : "", !neverRan(call));
  const trace = new MemoryTrace(run_id, () => new Date(), folded.entries);
  // The pool's own spend, which a resume was seeded with off the ledger.
  const budget = { get usd() { return harness.budget.spent.cost_usd; } };
  const model = new KernelModelClient({ cfg, tools: registry, domains: setup.survey.domains, survey: setup.survey.text, range }, turns);
  const deps: Deps = { model, tools: registry, trace, budget, domains: setup.survey.domains, sizes: setup.survey.sizes, clock: () => new Date() };

  // Mapped once; a resume hands the model the map it already paid for.
  let environment = setup.environment;
  if (folded.setup === null) {
    try {
      environment = await scope(deps, cfg, range);
    } catch (error) {
      if (signal.aborted) return halted(signal, end, state, run_id, event);
      if (spentOut(error)) return end("budget_exhausted", error.message);
      log(`environment mapping failed: ${String(error)}`);
      environment = null;
    }
    setup = { ...setup, environment };
    await state.append(run_id, [event("rootcause_setup", setup)]);
  } else if (environment !== null) {
    await scope(deps, cfg, range, environment);
  }

  const graph = folded.graph ?? seedFromAlert(createGraph(run_id), alert, cfg);
  const fresh = folded.lastTurn < 0;
  let exit: ExitReason | null = null;
  let report: string;
  try {
    const result = await run(alert, cfg, deps, {
      runId: run_id,
      range,
      index,
      graph,
      startTurn: Math.max(1, folded.lastTurn + 1),
      signal,
      log,
      onTurn: async (g, turn) => {
        turnNow = turn + 1;
        if (turn === 0 && !fresh) return;
        await state.append(run_id, [event("rootcause_turn", { turn, ...(turn === 0 && g.rare !== undefined ? { rare: g.rare } : {}), entries: trace.drain() })]);
      },
    });
    exit = result.exit;
    report = result.report;
  } catch (error) {
    // What the run found so far is the report, however it stopped.
    // A run the pool cut off ends budget-exhausted; a stop or a failure has no exit.
    const cut = !signal.aborted && spentOut(error) ? "budget-exhausted" : null;
    const written = renderReport(graph, trace, cut, alert, budget);
    if (signal.aborted && !(signal.reason instanceof Stopped)) return { status: "lost", reason: String(signal.reason) };
    await state.append(run_id, [event("rootcause_exit", { exit: cut, report: written, entries: trace.drain() })]);
    if (signal.aborted) return halted(signal, end, state, run_id, event, conclusionOf(written));
    if (spentOut(error)) return end("budget_exhausted", error.message, conclusionOf(written));
    return end("failed", error instanceof Error ? error.message : String(error), conclusionOf(written));
  }
  await state.append(run_id, [event("rootcause_exit", { exit, report, entries: trace.drain() })]);
  const outcome: RunOutcome = exit === "budget-exhausted" ? "budget_exhausted" : "completed";
  return end(outcome, `the trace ended: ${exit}`, conclusionOf(report));
}

const argsOf = (raw: string): unknown => { try { return JSON.parse(raw); } catch { return {}; } };

// Framed, scoped and surveyed: everything the loop needs before its first search.
async function open(options: V3Options, adapter: SourceAdapter, invoke: Invoke, turns: KernelTurns): Promise<SetupPayload> {
  const { spec, run_id } = options;
  const configured = typeof spec.sections["root_cause_index"] === "string" ? (spec.sections["root_cause_index"] as string) : null;
  const indexes = configured === null ? await adapter.indexes(invoke) : [configured];
  const framed = await frameAlert(turns, run_id, spec.prompt, indexes);
  const index = configured ?? framed.index;
  if (index === null) throw new Unframed(`the brief does not say which of the indexes holds its telemetry (${indexes.slice(0, 20).join(", ") || "none listed"}), and no root_cause_index is configured`);
  const t = Date.parse(framed.alert.observedAt);
  const range: [string, string] = [new Date(t - defaultConfig.lookbackDays * DAY_MS).toISOString(), new Date(t + DAY_MS).toISOString()];
  const survey = await adapter.open(invoke, { index, range }).survey();
  return { cfg: configFor(spec), alert: framed.alert, index, range, survey, environment: null };
}

async function halted(
  signal: AbortSignal,
  end: (outcome: RunOutcome, reason: string, summary?: string) => Promise<V3Report>,
  state: State3,
  runId: string,
  event: (kind: string, payload: unknown) => NewEvent<V3Kinds>,
  summary?: string,
): Promise<V3Report> {
  const why = signal.reason;
  if (!(why instanceof Stopped)) return { status: "lost", reason: String(why) };
  await state.append(runId, [event("directive", why.directive)]);
  const text = why.directive.text.trim();
  return end("aborted", text === "" ? "an operator stopped the run" : `an operator stopped the run: ${text}`, summary);
}

// What the console shows as the run's deliverable: the investigator's own conclusion,
// else the report's opening.
export function conclusionOf(report: string): string {
  const lines = report.split("\n");
  const at = lines.findIndex((line) => line.startsWith("## Investigator's conclusion"));
  if (at < 0) return report.trim().slice(0, 4_000);
  const next = lines.findIndex((line, i) => i > at && line.startsWith("## "));
  return lines.slice(at + 1, next < 0 ? undefined : next).join("\n").trim().slice(0, 4_000);
}
