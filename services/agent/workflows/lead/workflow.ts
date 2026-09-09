import { createHash } from "node:crypto";
import pLimit from "p-limit";
import type { DispatchPayload, NewEvent, RunKind, RunOutcome } from "../../contracts/events.js";
import { journalAnswers, noAnswers, type Answers } from "../../core/answers.js";
import { announceOpen, noAnnounce, type Announce } from "../../core/checkpoints.js";
import { commitTurn, type Harness, type Outcome, type TurnConfig } from "../../core/loop.js";
import { drain, streamTurn } from "../../core/stream.js";
import { SpecError, type RoleSpec, type RunSpec } from "../../core/spec.js";
import { topologyFor, type Assignment, type Round } from "../../core/topology.js";

// What every arch's lead emits. Everything past action is arch-specific and read
// only when the arch declared it, so one loop drives a swarm and a single lead.
export interface Decision {
  action: string;
  rationale: string;
  worker_agent_id?: string | null;
  query_intent?: string;
}

export interface DecisionPayload {
  action: string;
  rationale: string;
  worker: string | null;
}

export interface FindingPayload {
  agent_id: string;
  answer: unknown;
}

export type LeadKinds = { decision: DecisionPayload; finding: FindingPayload };
type Event = NewEvent<LeadKinds>;

export interface LeadOptions {
  run_id: string;
  run_kind: RunKind;
  spec: RunSpec;
  actions: readonly string[];
  halts: readonly string[];
  started_by?: string;
  // Where an answer to a parked call comes from. Defaults to nobody, so a run
  // with no source parks and stays parked rather than proceeding unapproved.
  answers?: Answers;
  // How a human is told a run has parked. Defaults to nobody, and a checkpoint
  // nobody is told about is one nobody answers.
  announce?: Announce;
  // Aborted when this worker loses its lease. The provider request carries it, so
  // a worker that has been declared dead stops paying for a call nobody records.
  signal?: AbortSignal;
}

export interface LeadReport {
  status: RunOutcome | "waiting_approval";
  reason: string;
  iterations: number;
  dispatched: number;
  pending: Outcome<unknown>["pending"];
}

// Deny-by-default per role, straight off the arch: what the registry grants is
// what the arch declared, so a role never sees a catalogue.
export function grantsOf(spec: RunSpec): Record<string, readonly string[]> {
  const workers = Object.entries(spec.roles.workers).map(([id, role]) => [id, role.tools]);
  return {
    ...(spec.roles.lead === undefined ? {} : { lead: spec.roles.lead.tools }),
    ...(spec.roles.critic === undefined ? {} : { critic: spec.roles.critic.tools }),
    ...Object.fromEntries(workers),
  };
}

// The deterministic half, driven entirely by the arch: one decision per iteration,
// a worker turn when the decision names one, and an ending the model cannot force.
export async function runLead(harness: Harness<LeadKinds>, options: LeadOptions): Promise<LeadReport> {
  const { run_id, spec } = options;
  const lead = spec.roles.lead;
  // Refused at the door rather than per iteration: a lead-less arch belongs to a
  // workflow that sequences itself, and running it here would decide nothing.
  if (lead === undefined) throw new SpecError(`arch ${spec.arch} declares no lead, so it cannot run a lead loop`);
  if ((await harness.state.latestSeq(run_id)) === null) await open(harness, options);

  const topology = topologyFor(spec.dispatch.topology);
  const rounds: Round[] = [];
  for (;;) {
    // Per iteration, not once per resume: an answer journaled after the parked check
    // waits for one iteration boundary rather than a whole resume.
    await journalAnswers(harness.state, run_id, options.run_kind, options.answers ?? noAnswers);

    const outcome = await drain(streamTurn<Decision, LeadKinds>(turnFor(options, "lead", lead, brief(spec)), harness));
    if (outcome.status === "waiting_approval") {
      // Announced every time the run is looked at, not only when it first parks:
      // the far side is idempotent, and a notice lost to a restart is re-sent.
      if (outcome.pending !== null) {
        await announceOpen(harness.state, run_id, options.run_kind, outcome.pending.checkpoint_id, options.announce ?? noAnnounce);
      }
      return { ...(await report(harness, options)), status: "waiting_approval", reason: outcome.reason, pending: outcome.pending };
    }
    if (outcome.status === "failed" || outcome.value === null) {
      return end(harness, options, outcome.refusal === null ? "failed" : "budget_exhausted", outcome.reason);
    }

    const decision = outcome.value;
    const selection = { worker: named(spec, decision), task: decision.query_intent ?? decision.rationale };
    const assignments = topology.assign(selection, spec);
    await commitTurn(harness.state, run_id, [
      event(options, "decision", { action: decision.action, rationale: decision.rationale, worker: selection.worker }),
    ]);

    await dispatchAll(harness, options, assignments);
    rounds.push({ assigned: assignments.length });

    // Termination is the arch's, not the model's: an action outside the halting
    // set keeps the run going however confident the rationale sounds.
    if (options.halts.includes(decision.action)) return end(harness, options, "completed", decision.rationale);
    // The topology's own ending, which is a different claim: a swarm that has
    // gone quiet is finished whether or not anyone said so.
    if (topology.settled(rounds)) return end(harness, options, "completed", `the ${topology.id} went quiet`);
  }
}

// A worker the arch declares, or nothing. A name the roster does not hold is a
// decision the loop refuses to act on rather than one it invents a worker for.
function named(spec: RunSpec, decision: Decision): string | null {
  const id = decision.worker_agent_id;
  return typeof id === "string" && id in spec.roles.workers ? id : null;
}

// Turns may overlap and so may their writes; every position is the store's. What a
// round owes is order, so it commits in assignment order once the turns are in.
async function dispatchAll(harness: Harness<LeadKinds>, options: LeadOptions, assignments: readonly Assignment[]): Promise<void> {
  if (assignments.length === 0) return;
  if (options.spec.dispatch.mode === "serial") {
    for (const assignment of assignments) await write(harness, options, await turn(harness, options, assignment));
    return;
  }

  // Capped at what the arch allows to run at once, not at how many were assigned.
  const gate = pLimit(options.spec.dispatch.max_workers);
  const done = await Promise.all(assignments.map((assignment) => gate(() => turn(harness, options, assignment))));
  for (const result of done) await write(harness, options, result);
}

interface Dispatched {
  outcome: Outcome<unknown>;
  own: Event[];
}

async function turn(harness: Harness<LeadKinds>, options: LeadOptions, assignment: Assignment): Promise<Dispatched> {
  const { role: worker, task: intent } = assignment;
  const role = options.spec.roles.workers[worker] as RoleSpec;
  const outcome = await drain(streamTurn<unknown, LeadKinds>(turnFor(options, worker, role, intent), harness));

  const complete = outcome.status === "completed";
  const payload: DispatchPayload = {
    dispatch_id: `dsp-${digest(`${options.run_id}\n${worker}\n${intent}`)}`,
    agent_id: worker,
    status: complete ? "complete" : "failed",
    question_id: null,
    failure_reason: complete ? null : outcome.reason,
  };
  const own: Event[] = [event(options, "dispatch", payload)];
  if (outcome.value !== null) own.push(event(options, "finding", { agent_id: worker, answer: outcome.value }));
  return { outcome, own };
}

async function write(harness: Harness<LeadKinds>, options: LeadOptions, result: Dispatched): Promise<number> {
  return commitTurn(harness.state, options.run_id, result.own);
}

function turnFor(options: LeadOptions, role: string, spec: RoleSpec, task: string): TurnConfig {
  const { runtime } = options.spec;
  return {
    run_id: options.run_id,
    run_kind: options.run_kind,
    role,
    system: spec.prompt,
    task,
    schema: spec.output_schema,
    max_turns: runtime.max_turns,
    approvals: new Set(options.spec.approvals),
    verbs: options.actions,
    result_cap: runtime.result_cap,
    recall_limit: runtime.recall_limit,
    // Only the lead recalls at run start: its prefix is the run's, and a worker
    // reading again would open a second prefix on a neighbourhood that has moved.
    recall_keys: role === "lead" ? openingKeys(options.spec) : [],
    ...(options.signal === undefined ? {} : { signal: options.signal }),
  };
}

// What the run opens its episodic read on, stated by whoever created it. Sorted for
// the reason recallKeysOf sorts: one investigation asks in one order, whatever its source.
function openingKeys(spec: RunSpec): readonly string[] {
  const held = spec.sections["recall_keys"];
  if (!Array.isArray(held)) return [];
  return [...new Set(held.filter((key): key is string => typeof key === "string" && key !== ""))].sort();
}

// The playbook's half, rendered once: what this run is about and what an analyst
// should know. The fold that replaces it with a digest is a later slice.
function brief(spec: RunSpec): string {
  const objectives = spec.objectives.map((line) => `- ${line}`).join("\n");
  const parts = [`Run: ${spec.name}`, objectives && `Objectives:\n${objectives}`, spec.narrative];
  return parts.filter((part) => part).join("\n\n");
}

function event(options: LeadOptions, kind: Event["kind"], payload: Event["payload"]): Event {
  return { run_id: options.run_id, run_kind: options.run_kind, kind, payload };
}

async function open(harness: Harness<LeadKinds>, options: LeadOptions): Promise<void> {
  await harness.state.append(options.run_id, [
    event(options, "run", {
      run_kind: options.run_kind,
      spec: options.spec,
      budgets: harness.budget.limits,
      seed: options.run_id,
      tenant_id: null,
      started_by: options.started_by ?? "worker",
    }),
  ]);
}

async function end(harness: Harness<LeadKinds>, options: LeadOptions, outcome: RunOutcome, reason: string): Promise<LeadReport> {
  await harness.state.append(options.run_id, [event(options, "terminal", { outcome, reason })]);
  return { ...(await report(harness, options)), status: outcome, reason, pending: null };
}

// Folded rather than counted in a local, so a resumed run reports what the
// ledger holds and not what this process happened to do.
async function report(harness: Harness<LeadKinds>, options: LeadOptions): Promise<Omit<LeadReport, "status" | "reason" | "pending">> {
  const events = await harness.state.read(options.run_id);
  // Decisions, not model calls: the harness counts a call per turn and per
  // emission attempt, which is spend, while an arch's max_calls means these.
  return {
    iterations: events.filter((one) => one.kind === "decision").length,
    dispatched: events.filter((one) => one.kind === "dispatch").length,
  };
}

function digest(material: string): string {
  return createHash("sha256").update(material).digest("hex").slice(0, 12);
}
