import { Queue, UnrecoverableError, Worker } from "bullmq";
import { hostname } from "node:os";
import { randomUUID } from "node:crypto";
import pg from "pg";
import { archFor } from "./arch/registry.js";
import { httpAnswers, journalAnswers, noAnswers, type Answers } from "./core/answers.js";
import { httpAnnounce, noAnnounce, type Announce } from "./core/checkpoints.js";
import { harnessFor, internalToken, type HarnessFactory } from "./harness.js";
import { poolConfig, redisConfig } from "./core/db.js";
import { healthPort, healthServer } from "./core/health.js";
import { jobIdFor, RUN_QUEUE, JOB_SCHEMA_VERSION, type RunJob } from "./contracts/job.js";
import type { AgentEvent, CheckpointPayload, ResolutionPayload, RunPayload } from "./contracts/events.js";
import type { SpendPayload } from "./contracts/budget.js";
import {
  LEASE_TTL_MS,
  PARK_EVERY_MS,
  RENEW_EVERY_MS,
  SWEEP_EVERY_MS,
  type Leases,
} from "./core/leases.js";
import { LeaseRepository } from "./ledger/leases.js";
import { seedFrom } from "./core/budget.js";
import type { State } from "./core/seams.js";
import { httpPlaybooks, isReference, type PlaybookResolver } from "./core/playbooks.js";
import { assembleSpec, buildSpec, DEFAULT_BUDGETS, loadArch, parseConfig, parsePlaybook, SpecError, withOverrides, type RunSpec } from "./core/spec.js";
import { LedgerRepository } from "./ledger/repository.js";
import { httpMirror, nullMirror, type Mirror } from "./workflows/compose/mirror.js";
import { runCompose } from "./workflows/compose/workflow.js";
import type { ComposeKinds } from "./workflows/compose/vocabulary.js";
import { runLead, type LeadKinds } from "./workflows/lead/workflow.js";
import { runHunt } from "./workflows/hunt/workflow.js";
import type { HuntKinds } from "./workflows/hunt/ledger.js";
import type { DirectiveQueue } from "./workflows/hunt/ports.js";
import { InProcessDirectiveQueue } from "./workflows/hunt/directives.js";
import { DirectiveRepository } from "./ledger/directives.js";

type StartJob = Extract<RunJob, { reason: "start" }>;

// The registry is the only resolver, and it runs before the ledger opens: an
// unregistered run kind fails at startup rather than seven iterations in.
export async function resolveSpec(job: StartJob, resolve: PlaybookResolver = defaultResolver()): Promise<RunSpec> {
  const entry = archFor(job.run_kind);
  const arch = job.request.arch === "" ? entry.arch : job.request.arch;
  // Carried on the job, not the reference, which names a definition many runs share.
  const asked = job.request.hypotheses ?? [];
  const turns = job.request.iterations;
  // Only ever tightens: a caller may ask to be asked, never to skip a declared gate.
  const gate = job.request.approve_hypotheses === true ? { hypothesis_approval: "ask" } : {};
  const tighten = (spec: RunSpec): RunSpec =>
    withOverrides(
      {
        ...spec,
        sections: {
          ...spec.sections,
          ...(asked.length === 0 ? {} : { operator_hypotheses: asked }),
          ...(Object.keys(gate).length === 0
            ? {}
            : { checkpoints: { ...((spec.sections?.["checkpoints"] as object) ?? {}), ...gate } }),
        },
        // Under thresholds: the harness refuses an unknown budgets key, and turns are
        // the workflow's unit rather than its own.
        ...(turns === undefined ? {} : { thresholds: { ...spec.thresholds, max_iterations: turns } }),
      },
      job.request.overrides,
    );
  if (!isReference(job.request.playbook)) {
    return tighten(buildSpec({ arch, playbook: job.request.playbook, config: job.request.config }, entry.actions, entry.owned, job.request.prompt));
  }

  // A reference answers with both layers, so a config path beside one is a second
  // source for a layer that already has one rather than an override of it.
  if (job.request.config !== "") {
    throw new SpecError(`${job.request.playbook} resolves its own config, so ${job.request.config} has nothing to say`);
  }
  const layers = await resolve(job.request.playbook);
  return tighten(
    assembleSpec({
      arch: loadArch(arch, entry.actions),
      playbook: parsePlaybook(layers.playbook, entry.owned),
      config: parseConfig(layers.config, entry.owned),
      prompt: job.request.prompt,
    }),
  );
}

// Off unless a deployment says where to mirror to: a run whose progress nobody
// collects still runs, and the ledger is the record either way.
function mirrorFor(): Mirror {
  const url = process.env["VIGIL_RUNS_URL"];
  return url === undefined || url === "" ? nullMirror : httpMirror({ url, token: internalToken() });
}

// The same endpoint the compose mirror answers from, read-only. Off unless a
// deployment says where, so a run nobody can answer parks rather than proceeds.
function answersFor(): Answers {
  const url = process.env["VIGIL_RUNS_URL"];
  return url === undefined || url === "" ? noAnswers : httpAnswers({ url, token: internalToken() });
}

// Where a parked run's question goes. The same endpoint the answer comes back
// from, so a deployment that can answer is one that can be told there is one.
function announceFor(): Announce {
  const url = process.env["VIGIL_RUNS_URL"];
  return url === undefined || url === "" ? noAnnounce : httpAnnounce({ url, token: internalToken() });
}

function defaultResolver(): PlaybookResolver {
  return httpPlaybooks({
    url: process.env["VIGIL_PLAYBOOKS_URL"] ?? "http://localhost:6987/internal/playbooks",
    token: internalToken(),
  });
}

// The spec a run started under, read off the ledger. A resume re-reads no file,
// so an edited arch or config cannot reach a run already in flight.
//
// Budgets are backfilled from the defaults for keys the journaled spec predates.
// A run in flight when a new ceiling ships has never heard of it, and the arithmetic
// downstream is not defensive: Math.min against an undefined limit is NaN, which
// reaches Postgres as an interval it refuses, and the run then fails every sweep
// forever. Backfilling also keeps the promise above intact -- what a resume must not
// do is re-read the *file*, not decline to know a default.
export async function specOf(state: State, runId: string): Promise<RunSpec | null> {
  const opened = (await state.read(runId)).find((event) => event.kind === "run");
  if (opened === undefined) return null;
  const spec = (opened.payload as RunPayload).spec as RunSpec;
  return { ...spec, budgets: { ...DEFAULT_BUDGETS, ...spec.budgets } };
}

// A workflow's event kinds are its own and the repository is generic over them, so
// the ledger is retyped per branch rather than every workflow sharing one union.
function as<K extends Record<string, unknown>>(state: State): State<K> {
  return state as unknown as State<K>;
}

// The one place a run kind becomes a loop. A kind with no workflow throws here,
// before the ledger opens, rather than journalling a run nothing will ever advance.
async function drive(
  state: State,
  job: RunJob,
  spec: RunSpec,
  build: HarnessFactory,
  signal: AbortSignal,
  directives: DirectiveQueue,
): Promise<void> {
  const { run_kind: kind, run_id, enqueued_by: started_by } = job;
  // Folded off the ledger, so a resumed run continues its allowance instead of
  // starting one. Without this, a run killed near its ceiling comes back with a
  // full budget, and a watchdog that resumes it automatically makes that a loop.
  const seed = seedFrom(await state.read(run_id));

  if (kind === "compose") {
    await runCompose(build(kind, spec, as<ComposeKinds>(state), undefined, seed), { run_id, spec, started_by, mirror: mirrorFor(), signal });
    return;
  }
  const entry = archFor(kind);
  if (entry.workflow === "hunt") {
    const harness = build(kind, spec, as<HuntKinds>(state), undefined, seed);
    await runHunt(harness, { run_id, spec, actions: entry.actions, queue: directives, started_by, announce: announceFor(), signal });
    return;
  }
  if (kind === "hunt" || kind === "investigate") {
    const harness = build(kind, spec, as<LeadKinds>(state), undefined, seed);
    await runLead(harness, { run_id, run_kind: kind, spec, actions: entry.actions, halts: entry.halts, started_by, answers: answersFor(), announce: announceFor(), signal });
    return;
  }
  throw new SpecError(`no workflow is wired for run_kind ${kind}`);
}

// Resolves the spec on a start and reads it back off the ledger on a resume, so an
// edited arch never reaches a run in flight, then hands both to the workflow.
//
// The lease is taken here and given back here, and renewed by a timer rather than
// by the workflow: an iteration can sit on one model call for minutes, so renewal
// cannot hang off an iteration boundary. Losing it aborts the call in flight,
// because a worker that has been declared dead is paying for a response no one
// will record.
export async function advance(
  state: State,
  leases: Leases,
  job: RunJob,
  build: HarnessFactory = harnessFor,
  directives: DirectiveQueue = new InProcessDirectiveQueue(),
): Promise<void> {
  if ((await state.terminal(job.run_id)) !== null) {
    await leases.finish(job.run_id);
    return;
  }

  const owner = workerName();
  if (!(await leases.claim(job.run_id, job.run_kind, owner, LEASE_TTL_MS))) return;

  const halt = new AbortController();
  const renewing = setInterval(() => {
    void leases.renew(job.run_id, owner, LEASE_TTL_MS).then(
      (held) => {
        if (!held) halt.abort(new Error(LOST_LEASE));
      },
      // A renewal that could not be read is not a lost lease: the claim outlives
      // several attempts, and killing the run over one failed query would be worse
      // than the late renewal it is recovering from.
      () => {},
    );
  }, RENEW_EVERY_MS);

  try {
    const latest = await state.latestSeq(job.run_id);
    if (latest === null && job.reason !== "start") throw new Error(`cannot resume ${job.run_id}: it has no ledger`);

    const spec = latest === null ? await resolveSpec(job as StartJob) : await specOf(state, job.run_id);
    if (spec === null) throw new Error(`cannot advance ${job.run_id}: its ledger holds no run event`);

    // Before the park check, not after it: abandoning is irreversible, and a run
    // whose answer is sitting at the endpoint unjournaled has been answered. The
    // workflow journals again on every iteration; this is idempotent against what
    // the ledger already holds, so the second call appends nothing.
    await journalAnswers(state, job.run_id, job.run_kind, answersFor());

    if (await abandonIfParkedOut(state, leases, job, spec)) return;
    if (await abandonIfStalled(state, leases, job)) return;
    if (latest !== null) await markResumed(state, job, owner, latest);
    await drive(state, job, spec, build, halt.signal, directives);
    await settle(state, leases, job, spec, owner);
  } catch (error) {
    await abandon(job, error);
    await forget(state, leases, job, owner, error);
    throw error;
  } finally {
    clearInterval(renewing);
  }
}

// A run that failed before it opened a ledger is not a run: no terminal can be
// journaled for it and no resume can read it back, so its lease row would sit there
// being swept forever, throwing on every attempt. Dropped here because this is the
// only place that knows the ledger stayed empty.
//
// A failure with a ledger behind it keeps its row -- that run is real and
// unfinished -- but hands the claim back. Holding it out the TTL would refuse the
// retry that lands seconds later, and a refused claim returns quietly, so BullMQ
// would retire the job as a success with the run stalled. release() is scoped to
// this owner, so a worker already reclaimed displaces nobody.
async function forget(state: State, leases: Leases, job: RunJob, owner: string, error: unknown): Promise<void> {
  if ((await state.latestSeq(job.run_id)) === null) {
    await leases.finish(job.run_id);
    return;
  }
  if (await stopBecauseItCannotSucceed(state, leases, job, error)) return;
  await leases.release(job.run_id, owner, 0);
}

// A spec error answers the same way on every attempt, and on a resume the layers come
// off the ledger, so no retry can change it. Say why it stopped and stop, rather than
// refilling the queue with jobs none of which could succeed.
async function stopBecauseItCannotSucceed(
  state: State,
  leases: Leases,
  job: RunJob,
  error: unknown,
): Promise<boolean> {
  if (!(error instanceof SpecError)) return false;
  // SpecError is not only about specs: a lead that emits no decision throws one, often
  // because a checkpoint is open. A run waiting on a person must never be killed.
  if (await waitingOnSomeone(state, job.run_id)) return false;

  const reason = `its spec cannot be built: ${error.message}`;
  await state.append(job.run_id, [
    { run_id: job.run_id, run_kind: job.run_kind, kind: "terminal", payload: { outcome: "failed", reason } },
  ]);
  // abandon() already told the mirror for compose; every other kind is told off the
  // terminal in settle, which this path returns before reaching.
  if (job.run_kind !== "compose") {
    await mirrorFor().terminal(job.run_id, { outcome: "failed", reason, summary: "" });
  }
  await reap(leases, job.run_id);
  return true;
}

// Where a run was picked back up, so a crash and its recovery are readable rather
// than a silent gap. Skipped when the last event is itself a resume: a parked run
// is swept on every interval, and a mark per sweep would say nothing happened
// several hundred times.
async function markResumed(state: State, job: RunJob, owner: string, latest: number): Promise<void> {
  const [last] = await state.read(job.run_id, { since: latest - 1 });
  if (last?.kind === "resumed") return;
  await state.append(job.run_id, [
    { run_id: job.run_id, run_kind: job.run_kind, kind: "resumed", payload: { worker: owner, enqueued_by: job.enqueued_by } } as never,
  ]);
}

export const LOST_LEASE = "the lease was reclaimed by another worker";

// Which process, not which person: a directive's actor is who steered a run and
// this is what is holding it. Reported to the console, never read to decide.
function workerName(): string {
  return `${hostname()}:${process.pid}`;
}

// Whether this worker is still the run's business, once the workflow returns. A
// run that reached terminal is nobody's; one that parked stays on the list and is
// looked at again when its interval passes or the console pulls it forward.
async function settle(state: State, leases: Leases, job: RunJob, spec: RunSpec, owner: string): Promise<void> {
  const terminal = await state.terminal(job.run_id);
  if (terminal !== null) {
    // Only compose was ever handed a mirror, so every other kind reached its end
    // with nobody to tell: the console read workflow_runs and showed a finished
    // hunt as running forever, with no duration and no cost. Off the terminal
    // this already read, so one place reports for every kind there is.
    if (job.run_kind !== "compose") {
      await mirrorFor().terminal(job.run_id, {
        outcome: terminal.outcome,
        reason: terminal.reason,
        summary: terminal.summary ?? "",
        cost_usd: await spentOn(state, job.run_id),
        handoffs: terminal.handoffs ?? [],
      });
    }
    await reap(leases, job.run_id);
    return;
  }
  await leases.release(job.run_id, owner, Math.min(PARK_EVERY_MS, spec.budgets.max_park_ms));
}

// What the run cost, summed from the spend events every kind writes. Nothing was
// summing them, so a finished run showed a dash where its dollars should be.
// A call nobody could price contributes nothing rather than a fabricated zero.
export async function spentOn(state: State, runId: string): Promise<number> {
  const events = (await state.read(runId)) as readonly AgentEvent<Record<never, never>>[];
  return events
    .filter((event) => event.kind === "spend")
    .reduce((total, event) => total + ((event.payload as SpendPayload).cost_usd ?? 0), 0);
}

// A run that dies before it journals a terminal leaves its record open, and a
// resolution failure dies before there is a ledger to journal one onto.
async function abandon(job: RunJob, error: unknown): Promise<void> {
  if (job.run_kind !== "compose") return;
  await mirrorFor().terminal(job.run_id, { outcome: "failed", reason: error instanceof Error ? error.message : String(error), summary: "" });
}

// A checkpoint nobody answered for max_park_ms. Saying so beats leaving the run
// parked forever, and abandoned is not aborted: aborted means a human stopped the
// run, and this is the case where nobody decided anything at all.
async function abandonIfParkedOut(state: State, leases: Leases, job: RunJob, spec: RunSpec): Promise<boolean> {
  const events = await state.read(job.run_id);
  const waited = parkedFor(events);
  if (waited === null || waited < spec.budgets.max_park_ms) return false;

  const days = (waited / 86_400_000).toFixed(1);
  const reason = `parked ${days} days without an answer`;
  await state.append(job.run_id, [
    { run_id: job.run_id, run_kind: job.run_kind, kind: "terminal", payload: { outcome: "abandoned", reason } },
  ]);
  // This path returns before settle, so nobody reported it: the console showed the
  // run paused forever and its question stayed in the approvals queue for good.
  await mirrorFor().terminal(job.run_id, { outcome: "abandoned", reason, summary: "" });
  await reap(leases, job.run_id);
  return true;
}

// Sweeps that journal nothing but being picked up again: a run whose calls fail without
// throwing advances nothing, and the SpecError path cannot see it because nothing threw.
// Counted off the ledger, which is what survives a worker restart.
export const MAX_STALLED_RESUMES = 6;

// Neither is progress: a failing call writes a spend at zero, and a resume says only
// that somebody looked again.
const NOT_PROGRESS: ReadonlySet<string> = new Set(["resumed", "spend"]);

async function abandonIfStalled(state: State, leases: Leases, job: RunJob): Promise<boolean> {
  const events = await state.read(job.run_id);
  // A parked hunt journals a resume on every sweep, so counting those would end every
  // run that asked a question. abandonIfParkedOut owns the case nobody answers.
  if (parkedFor(events) !== null) return false;
  let resumes = 0;
  for (let at = events.length - 1; at >= 0; at -= 1) {
    const kind = events[at]?.kind ?? "";
    if (!NOT_PROGRESS.has(kind)) break;
    if (kind === "resumed") resumes += 1;
  }
  if (resumes < MAX_STALLED_RESUMES) return false;

  const reason = `picked up ${resumes} times without advancing; something upstream is failing without saying so`;
  await state.append(job.run_id, [
    { run_id: job.run_id, run_kind: job.run_kind, kind: "terminal", payload: { outcome: "abandoned", reason } },
  ]);
  await mirrorFor().terminal(job.run_id, { outcome: "abandoned", reason, summary: "" });
  await reap(leases, job.run_id);
  return true;
}

// Whether anything is waiting on a person. Read fresh, since the callers reach here
// from a catch holding events that may predate the checkpoint.
async function waitingOnSomeone(state: State, runId: string): Promise<boolean> {
  return parkedFor(await state.read(runId)) !== null;
}

// How long the oldest unanswered checkpoint has been waiting, or null when none
// is. Read off the ledger's own timestamps, which is the one thing in this ticket
// that folds ts -- and the reason only the store may stamp it.
function parkedFor(events: readonly AgentEvent<Record<never, never>>[]): number | null {
  const answered = new Set(
    events.filter((one) => one.kind === "resolution").map((one) => (one.payload as ResolutionPayload).checkpoint_id),
  );
  const open = events.filter(
    (one) => one.kind === "checkpoint" && !answered.has((one.payload as CheckpointPayload).checkpoint_id),
  );
  const raised = open.map((one) => Date.parse(one.ts)).filter((at) => Number.isFinite(at));
  return raised.length === 0 ? null : Date.now() - Math.min(...raised);
}

// What a finished run leaves behind, which is less than the word suggests. Remote
// dispatch is request and response with a timeout, so no far side holds work of
// ours; dispatch events are journaled after their outcome rather than before it;
// and the approval mirror ADR 0003 describes is not written by this layer yet. So
// the lease row is the only thing to drop.
//
// Not the run's queued directives. Deleting them would take away the record of an
// instruction that never reached the run, which #634 is supposed to surface, and
// journaling them first would mean appending after a terminal -- a property worth
// less than the tidiness. agent_directives has no retention and neither does
// agent_events: one reaper for both is its own job, as the ADR already says.
async function reap(leases: Leases, runId: string): Promise<void> {
  await leases.finish(runId);
}


// A run whose claim has lapsed, put back on the queue. Two cases reach here and
// neither needs telling apart: a worker died holding the run, or a parked run's
// interval passed. Both mean "look at this again", and advance() reads the ledger
// to decide which it is.
//
// Nothing distinguishes a sweep from a start to the queue either, so the resume
// carries no request -- the ledger holds the spec, and the RunJob union makes a
// resume that read one fail to compile.
// Structurally what the sweeper needs of a queue, so it is drivable without a
// Redis. BullMQ's Queue satisfies it as it stands.
export interface Enqueue {
  add(name: string, job: RunJob, options: JobOptions): Promise<unknown>;
}

// What every enqueue of a run carries, wherever it is enqueued from -- here, or
// core/agents/queue.py, which sets the same two.
//
// BullMQ defaults to one attempt, so a job that throws is permanently failed and
// nothing rescues it: the sweeper below sweeps lapsed lease rows, and a job that
// died on its way into leases.claim never wrote one. Transient Postgres and Redis
// failures are rare on a dev box and routine under Kubernetes.
//
// Retrying is safe rather than merely tolerable: advance() checks terminal first
// and leases.claim is a conditional UPDATE, so a second attempt takes exactly the
// path a sweeper resume takes.
// Reachable because forget() hands the lease back; see there.
export const RUN_ATTEMPTS = 3;
export const RUN_BACKOFF = { type: "exponential", delay: 5_000 } as const;

interface JobOptions {
  jobId: string;
  attempts: number;
  backoff: { type: string; delay: number };
}

// The reservation lasts a lease TTL because that is the order of magnitude a job
// spends between being queued and being picked up. It is not ownership: the row
// keeps a null owner, so the worker that dequeues the job can claim it, and if no
// worker ever does the run is offered again once the reservation lapses.
export async function sweepOnce(leases: Leases, queue: Enqueue, limit = 50): Promise<number> {
  const due = await leases.sweep(LEASE_TTL_MS, limit);
  for (const claim of due) {
    const job: RunJob = {
      schema_version: JOB_SCHEMA_VERSION,
      run_id: claim.run_id,
      run_kind: claim.run_kind,
      tenant_id: null,
      enqueued_at: new Date().toISOString(),
      enqueued_by: "watchdog",
      reason: "resume",
    };
    await queue.add(RUN_QUEUE, job, { jobId: jobIdFor(job, randomUUID()), attempts: RUN_ATTEMPTS, backoff: { ...RUN_BACKOFF } });
  }
  return due.length;
}

// advance(), with the one distinction the retry policy above needs to be told
// about: a spec that does not parse parses no better on the third attempt. Retries
// exist for the infrastructure going away mid-run, not for a malformed playbook, and
// UnrecoverableError is how BullMQ is told which is which -- the job fails on
// attempt 1 and the console sees the real message rather than the same one thrice,
// fifteen seconds apart.
//
// Only here, not inside advance(): what a failure means to the queue is the queue's
// business, and advance() is driven without one by the tests and by run-once.ts.
async function handle(state: State, leases: Leases, job: RunJob, directives: DirectiveQueue, build: HarnessFactory = harnessFor): Promise<void> {
  try {
    await advance(state, leases, job, build, directives);
  } catch (error) {
    if (error instanceof SpecError) throw new UnrecoverableError(error.message);
    throw error;
  }
}

// How many runs one worker drives at once. Tunable because the right number is a
// deployment's model quota divided by what a run asks of it, which this cannot know.
export const DEFAULT_RUN_CONCURRENCY = 4;

export function runConcurrency(): number {
  const asked = Number(process.env["VIGIL_RUN_CONCURRENCY"]);
  return Number.isInteger(asked) && asked > 0 ? asked : DEFAULT_RUN_CONCURRENCY;
}

export interface Running {
  worker: Worker<RunJob>;
  ledger: LedgerRepository;
  leases: LeaseRepository;
  close: () => Promise<void>;
}

// The queue drains durable runs and the HTTP surface serves chat, which is
// synchronous and would gain nothing from a queue hop but latency. One process,
// one pool, two ways in.
// The factory is a parameter so a test can stand the model in and leave the
// queue, the ledger and the leases real, which is what the seam is made of.
export function startWorker(build: HarnessFactory = harnessFor): Running {
  const pool = new pg.Pool(poolConfig());
  const ledger = new LedgerRepository(pool);
  const leases = new LeaseRepository(pool);
  const connection = redisConfig();

  // Long, because the lease is the liveness signal and a second clock that
  // disagreed with it would either double-process a run or wedge one. BullMQ's
  // stalled sweep still retires a dead worker's job eventually, and the lease
  // refuses it if it comes back around.
  const directives = new DirectiveRepository(pool);
  // BullMQ defaults to one, so a single hunt held the queue for its whole life and
  // every other run waited it out. A run is almost entirely waiting on a model, so
  // the ceiling is the provider's rate limit rather than this process's CPU -- the
  // limiter already holds that line, and the lease keeps two workers off one run.
  const worker = new Worker<RunJob>(RUN_QUEUE, (job) => handle(ledger, leases, job.data, directives, build), {
    connection,
    concurrency: runConcurrency(),
    lockDuration: LEASE_TTL_MS * 10,
  });

  // A plain interval rather than a repeatable job: a repeat key lives in Redis and
  // can be lost on a deploy, and a watchdog that has silently stopped looks exactly
  // like one with nothing to do. This cannot stop while the process lives.
  //
  // Several replicas all sweep. That is harmless rather than merely tolerable --
  // the claim is one conditional UPDATE, so only the sweeper that wins a row
  // enqueues it.
  const producer = new Queue<RunJob>(RUN_QUEUE, { connection });
  const sweeping = setInterval(() => {
    void sweepOnce(leases, producer).catch((error: unknown) => {
      // A sweep that could not read the table tries again next tick. Throwing here
      // would take the process down with every run on it. Said out loud because a
      // watchdog failing every tick looks exactly like one with nothing to do.
      console.warn(`sweep failed: ${error instanceof Error ? error.message : String(error)}`);
    });
  }, SWEEP_EVERY_MS);

  return { worker, ledger, leases, close: async () => {
    clearInterval(sweeping);
    await worker.close();
    await producer.close();
    await pool.end();
  } };
}

// Whether this worker is worth sending work to. `status` rather than a ping:
// ioredis queues commands while it is offline, so a ping against a dropped
// connection would sit in that queue until the probe timed out -- reporting a hang
// where the connection state already says "not ready" for free.
export function workerReady(worker: Worker<RunJob>): () => Promise<boolean> {
  return async () => {
    if (!worker.isRunning()) return false;
    return (await worker.client).status === "ready";
  };
}

// Both the mirror and the answers endpoint hang off this one variable, and both go
// quietly inert when it is unset: runs still succeed, no phase progress ever reaches
// the console, and a run that stops for a human parks until it is abandoned because
// nothing can carry the decision back. Said once at startup, because nothing later
// in the run says it -- a silently-off mirror looks exactly like a quiet one.
function warnIfUnmirrored(): void {
  const url = process.env["VIGIL_RUNS_URL"];
  if (url !== undefined && url !== "") return;
  console.warn(
    "VIGIL_RUNS_URL is unset: run progress will not be mirrored to the backend and checkpoints cannot be answered",
  );
}

if (process.argv[1] !== undefined && import.meta.url.endsWith(process.argv[1].split("/").pop() ?? "")) {
  warnIfUnmirrored();
  const running = startWorker();
  // Probes only. Chat moved to serve.ts when #635 split the two into Deployments
  // that scale on different things -- a queue's depth and an open connection count
  // have nothing to say to each other.
  const probes = healthServer(workerReady(running.worker)).listen(healthPort());
  const stop = () => {
    probes.close();
    void running.close();
  };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}
