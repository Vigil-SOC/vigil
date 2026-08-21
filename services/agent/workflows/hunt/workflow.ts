import { announceOpen, noAnnounce, type Announce } from "../../core/checkpoints.js";
import { GatewayExhausted } from "../../core/limiter.js";
import type { Harness } from "../../core/loop.js";
import type { RunOutcome, TerminalHandoff } from "../../contracts/events.js";
import type { RunSpec } from "../../core/spec.js";
import { BudgetRefused, disconfirmationCritic, decisionProvider, narrativeWriter, workerDispatcher } from "./adapters.js";
import { narrativeInput, type Narrative } from "./narrative.js";
import type { Narrator } from "./ports.js";
import { huntSpec, verdictsOf } from "./config.js";
import { pendingCheckpoints } from "./checkpoints.js";
import { HuntAlreadyTerminal, HuntController, HuntParked, resumeHunt, startHunt } from "./controller.js";
import { createEnricher, type Tool } from "./enrich.js";
import { fold, type HuntEvent, type HuntKinds, type Projection } from "./ledger.js";
import type { HarnessFactory } from "../../harness.js";
import type { State } from "../../core/seams.js";
import type { DirectiveQueue } from "./ports.js";
// Aliased: this module's own HuntReport is the run's status, not the deliverable.
import { buildReport, renderReport, type HuntReport as HuntDeliverable } from "./report.js";
import type { Handoff, HuntOutcome } from "./types.js";
import { expandFrom } from "./expand.js";
import { registryOf } from "../../core/registry.js";
import { toolsFrom } from "../../tools/remote.js";
import { grantsOf } from "../lead/workflow.js";

export interface HuntOptions {
  run_id: string;
  spec: RunSpec;
  actions: readonly string[];
  queue: DirectiveQueue;
  started_by?: string;
  announce?: Announce;
  signal?: AbortSignal;
}

export interface HuntReport {
  status: RunOutcome | "waiting_approval";
  reason: string;
  iterations: number;
}

// The hunt on the harness. The controller owns every decision this makes; what
// is here is only what starts it, what stops it, and who is told when it parks.
export async function runHunt(harness: Harness<HuntKinds>, options: HuntOptions): Promise<HuntReport> {
  const spec = huntSpec(options.spec);
  const { run_id, queue } = options;

  // Resume when the ledger already holds one, so an edited arch cannot reach a
  // hunt in flight and the hypotheses are not re-opened on every attempt.
  const opened = (await harness.state.latestSeq(run_id)) !== null;
  let ledger;
  try {
    ledger = opened ? (await resumeHunt(harness.state, queue, run_id)).ledger : await startHunt(harness.state, queue, run_id, spec, options.started_by ?? "worker");
  } catch (error) {
    // Its own projection ended but the domain-free terminal is missing, so the
    // run is over for the hunt and not for anyone else. Settle rather than throw.
    if (!(error instanceof HuntAlreadyTerminal)) throw error;
    await harness.state.append(run_id, [
      { run_id, run_kind: "hunt", kind: "terminal", payload: { outcome: "completed", reason: error.message } } as never,
    ]);
    return { status: "completed", reason: error.message, iterations: 0 };
  }

  // The registry harnessFor built resolved every tool remotely, which cannot serve
  // one whose answer is this run's own ledger. Rebuilt here, where the run is known.
  const scoped: Harness<HuntKinds> = {
    ...harness,
    registry: registryOf(
      toolsFrom(options.spec.tools, { expand: expandFrom(harness.state, run_id) }),
      grantsOf(options.spec),
    ),
  };

  // The pool was built from the spec this run started on, so a resumed hunt would be
  // refused on the ceiling an extension already lifted. Only the two arms an extension
  // buys: budgetsOf owns the call meter and turn count.
  const granted = ledger.projection.hunt.budgets;
  harness.budget.raise({ max_cost_usd: granted.max_cost_usd, max_wall_ms: granted.max_wall_ms });

  const ports = { harness: scoped, spec: options.spec, run_id, actions: options.actions, ...(options.signal === undefined ? {} : { signal: options.signal }) };
  const narrator = narrativeWriter(ports);
  const controller = new HuntController(
    ledger,
    decisionProvider(ports),
    workerDispatcher(ports),
    options.spec.dispatch,
    spec.sections?.["digest"] as never,
    createEnricher(spec, enrichmentTools(scoped, options.spec)),
    disconfirmationCritic(ports),
    // verdictsOf reads the deployment's own thresholds, which the predicate must see.
    verdictsOf(options.spec),
    harness.budget,
  );

  for (;;) {
    // Handing the run back, not ending it. This signal fires for exactly one
    // reason -- renewal found another worker holding the lease -- so that worker
    // is driving the run now, and a terminal written here would end the run it is
    // in the middle of. An operator's abort is a directive and terminates above.
    if (options.signal?.aborted === true) return report(ledger, "aborted", "the worker lost its lease");
    try {
      const iteration = await controller.advanceIteration();
      // The controller buffers an iteration and the caller makes it durable: a
      // crash between the two loses the iteration rather than half of it.
      await ledger.flush();
      if (iteration.hunt_status === "terminal") {
        return await end(harness, options, ledger, outcomeOf(iteration.hunt_outcome), iteration.note, controller, narrator);
      }
    } catch (error) {
      // Parked is not failed: the hunt is waiting on a person, and the whole
      // point of announcing is that the person can be found.
      if (error instanceof HuntParked) {
        await ledger.flush();
        return await parked(harness, options, ledger, error.message);
      }
      // A run that spent its allowance has neither failed nor necessarily finished, so
      // it parks on the same extend/conclude/abort the hunt's own ceiling offers.
      if (error instanceof BudgetRefused) {
        const note = controller.parkOnRefusal(error.message);
        await ledger.flush();
        return await parked(harness, options, ledger, note);
      }
      if (error instanceof HuntAlreadyTerminal) return report(ledger, "completed", error.message);
      // Everything else has still left a ledger with iterations on it, so it ends here
      // through the same end() the budget path takes rather than being rethrown.
      const reason = error instanceof Error ? error.message : String(error);
      await ledger.flush();
      // The gateway's own allowance is not this hunt failing.
      if (error instanceof GatewayExhausted) {
        return await end(harness, options, ledger, "budget_exhausted", reason, controller, narrator);
      }
      // Logged as well as journaled: the stack is the only thing that says a defect in
      // this process from an ordinary failed run.
      console.error(`hunt ${run_id} ended on an unhandled error`, error);
      return await end(harness, options, ledger, "failed", reason, controller, narrator);
    }
  }
}

// An account of a run, written now, from the spec the run itself was built with --
// not from whatever the arch files say today. Standalone so a caller needs neither
// the lease nor the loop: a finished hunt is the one whose write-up a person most
// wants rewritten, and by then there is nothing left running to ask.
export async function narrateRun(
  state: State<HuntKinds>,
  runId: string,
  events: readonly HuntEvent[],
  build: HarnessFactory,
): Promise<Narrative> {
  const projection = fold(events);
  const spec = projection.hunt.spec;
  const harness = build<HuntKinds>("hunt", spec, state);
  const narrative = await narrativeWriter({ harness, spec, run_id: runId, actions: [] }).narrate(
    narrativeInput(projection, buildReport(projection)),
  );
  await state.append(runId, [
    { run_id: runId, run_kind: "hunt", kind: "narrative", payload: narrative } as never,
  ]);
  return narrative;
}

// The hunt ends by patching its own state, which only its projection reads. The
// lease, the API and the sweeper read the domain-free terminal, so it is written here.
async function end(
  harness: Harness<HuntKinds>,
  options: HuntOptions,
  ledger: Awaited<ReturnType<typeof startHunt>>,
  outcome: RunOutcome,
  reason: string,
  controller: HuntController,
  narrator?: Narrator,
): Promise<HuntReport> {
  // Through the controller even when the hunt stopped on something it never chose:
  // terminate() resolves the active hypotheses, stamps the outcome and finalizes.
  // Idempotent -- outcome precedence leaves an already-terminated hunt as it stands.
  controller.terminate(huntOutcomeOf(outcome), reason);
  await ledger.flush();
  const built = buildReport(ledger.projection);
  const narrative = await narrate(harness, options, ledger.projection, built, narrator);
  if ((await harness.state.terminal(options.run_id)) === null) {
    const handoffs = await handoffsOf(harness, options.run_id, ledger.projection);
    await harness.state.append(options.run_id, [
      { run_id: options.run_id, run_kind: "hunt", kind: "terminal", payload: { outcome, reason, summary: renderReport(built, ledger.projection, narrative), handoffs } } as never,
    ]);
  }
  return report(ledger, outcome, reason);
}

// One call, at the end, over the whole record. Fail-open, since the verdicts stand on
// their own. Journaled as its own event, so a regenerate is cheap and buildReport's
// frozen object -- the ADR 0012 goldens -- stays untouched.
async function narrate(
  harness: Harness<HuntKinds>,
  options: HuntOptions,
  projection: Projection,
  built: HuntDeliverable,
  narrator?: Narrator,
): Promise<Narrative | null> {
  if (narrator === undefined) return null;
  try {
    const narrative = await narrator.narrate(narrativeInput(projection, built));
    await harness.state.append(options.run_id, [
      { run_id: options.run_id, run_kind: "hunt", kind: "narrative", payload: narrative } as never,
    ]);
    return narrative;
  } catch (error) {
    console.error(`hunt ${options.run_id} ended without an account of it`, error);
    return null;
  }
}

// The case files the hunt wrote, carried out on the terminal. Read off the events
// rather than the fold, which keeps a handoff only as a mark on its hypothesis.
async function handoffsOf(harness: Harness<HuntKinds>, runId: string, projection: Projection): Promise<TerminalHandoff[]> {
  const events = await harness.state.read(runId);
  return events
    .filter((event) => event.kind === "handoff")
    .map((event) => event.payload as Handoff)
    .map((handoff) => ({
      case_id: handoff.case_id,
      title: projection.hypotheses.get(handoff.hypothesis_id)?.statement ?? handoff.rationale,
      markdown: handoff.case_markdown ?? handoff.case_file ?? handoff.rationale,
    }));
}

// What the workers were granted and nothing else: a chain runs with no decision
// behind it, so it must not reach a tool no role may call.
function enrichmentTools(harness: Harness<HuntKinds>, spec: RunSpec): Tool[] {
  const seen = new Map<string, Tool>();
  for (const role of Object.keys(spec.roles.workers)) {
    for (const tool of harness.registry.granted(role)) {
      if (seen.has(tool.id)) continue;
      seen.set(tool.id, {
        id: tool.id,
        description: tool.description,
        parameters: tool.parameters,
        run: async (args: Record<string, unknown>) => {
          const result = await harness.dispatch.invoke(tool, args);
          return result.ok ? JSON.stringify(result.rows) : `failed: ${result.failure.kind}`;
        },
      });
    }
  }
  return [...seen.values()];
}

async function parked(
  harness: Harness<HuntKinds>,
  options: HuntOptions,
  ledger: Awaited<ReturnType<typeof startHunt>>,
  reason: string,
): Promise<HuntReport> {
  const [open] = pendingCheckpoints(ledger.projection);
  if (open !== undefined) {
    await announceOpen(harness.state, options.run_id, "hunt", open.checkpoint_id, options.announce ?? noAnnounce);
  }
  return report(ledger, "waiting_approval", reason);
}

// The hunt's own outcomes, as the ledger's. inconclusive is a completed run that
// reported honestly, not a failure: saying nothing was shown is the point.
function outcomeOf(outcome: string | null): RunOutcome {
  if (outcome === "aborted") return "aborted";
  // budget_terminated is the hunt's name for it, budget_exhausted the shared one.
  if (outcome === "budget_terminated" || outcome === "budget_exhausted") return "budget_exhausted";
  if (outcome === "failed") return "failed";
  return "completed";
}

// The same translation the other way. abandoned has no hunt outcome of its own: the
// controller calls a run nobody answered before its park TTL aborted.
function huntOutcomeOf(outcome: RunOutcome): HuntOutcome {
  if (outcome === "budget_exhausted") return "budget_terminated";
  if (outcome === "aborted" || outcome === "abandoned") return "aborted";
  if (outcome === "failed") return "failed";
  return "completed";
}

function report(ledger: Awaited<ReturnType<typeof startHunt>>, status: HuntReport["status"], reason: string): HuntReport {
  return { status, reason, iterations: ledger.projection.hunt.iteration };
}
