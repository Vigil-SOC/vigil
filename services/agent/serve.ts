import { createHash, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import pg from "pg";
import { archFor, isHuntLike, registeredKinds } from "./arch/registry.js";
import { cachedReady, handleHealth, type Ready } from "./core/health.js";
import { LedgerRepository } from "./ledger/repository.js";
import { verifyLedger, type VerifyResult } from "./ledger/verify.js";
import { poolConfig } from "./core/db.js";
import { errorFields, logger } from "./core/log.js";
import type { RunKind } from "./contracts/events.js";
import type { ToolPrincipal } from "./contracts/tool.js";
import { FRESH } from "./core/budget.js";
import { nullMemory, recalling } from "./core/memory.js";
import type { Memory, State } from "./core/seams.js";
import { assembleSpec, loadArch, parseConfig, parsePlaybook, SpecError, type Playbook, type RunSpec } from "./core/spec.js";
import { chatEvents, sse } from "./workflows/chat/sse.js";
import { runChat, type Turn } from "./workflows/chat/workflow.js";
import { harnessFor, type HarnessFactory } from "./harness.js";
import { narrateRun } from "./workflows/hunt/workflow.js";
import type { HuntEvent, HuntKinds } from "./workflows/hunt/ledger.js";
import { replay, type ReplayReport } from "./workflows/hunt/replay.js";
import { investigateReplay } from "./workflows/lead/replay.js";

const log = logger("agent.serve");

const CHAT = "/chat/stream";
// GET /runs/<id>/projection -- what a supervisor outside this process reads.
const PROJECTION = /^\/runs\/([0-9a-fA-F-]{36})\/projection$/;
// GET /runs/<id>/distil -- what episodic memory reads once the run has ended.
const DISTIL = /^\/runs\/([0-9a-fA-F-]{36})\/distil$/;
const NARRATE = /^\/runs\/([0-9a-fA-F-]{36})\/narrate$/;
// GET /runs/<id>/replay[?decision_id=...] -- what each decision was shown, rebuilt
// from the ledger. Matched on the pathname, since this one takes a query.
const REPLAY = /^\/runs\/([0-9a-fA-F-]{36})\/replay$/;
// GET /runs/<id>/verify -- the hash chain, walked by verifyLedger. Python forwards it.
const VERIFY = /^\/runs\/([0-9a-fA-F-]{36})\/verify$/;
// GET /runs/<id>/events[?snapshots=1] -- the ledger itself. Snapshots stay off
// unless asked: a decision snapshot is the digest, and the case record does not show it.
const EVENTS = /^\/runs\/([0-9a-fA-F-]{36})\/events$/;
// A conversation is prose and a config, not an upload. Anything larger is a
// mistake or an attack, and either way it is refused before it is parsed.
const MAX_BODY = 1_000_000;

export interface ChatRequest {
  run_id: string;
  turns: readonly Turn[];
  // Resolved by the caller, which is the side that knows what an agent is. It
  // layers onto the arch prompt rather than replacing the house rules.
  system_prompt: string;
  // The config layer as YAML: model, budgets, runtime and the tools this
  // conversation may reach. Assembled per request, so it arrives per request.
  config: string;
  parent_run_id?: string;
  // Signed by the API for the person in this conversation and handed back on
  // every tool call. Absent means no person, and the tools record "agent".
  principal?: ToolPrincipal;
}

export function chatSpec(request: ChatRequest): RunSpec {
  const entry = archFor("chat");
  const playbook = parsePlaybook("");
  return assembleSpec({
    arch: loadArch(entry.arch, entry.actions),
    playbook: directed(playbook, request.system_prompt),
    config: parseConfig(request.config),
    prompt: "",
  });
}

// Through the directive layer the arch already has, so the caller's prompt is
// appended to the house rules rather than swapped for them.
function directed(playbook: Playbook, prompt: string): Playbook {
  return prompt.trim() === "" ? playbook : { ...playbook, directives: { ...playbook.directives, lead: prompt } };
}

// What the parent carries forward, if it carries anything. An unknown kind, an
// absent ledger and a kind with no renderer all recall nothing rather than fail.
export async function memoryFor(state: State, parentRunId: string | undefined): Promise<Memory> {
  if (parentRunId === undefined || parentRunId === "") return nullMemory;
  // Off the envelope rather than the seq-0 payload, whose shape depends on which
  // entry point opened the run. run_kind is on every event either way.
  const [opened] = await state.read(parentRunId);
  if (opened === undefined || !registeredKinds().includes(opened.run_kind)) return nullMemory;

  const notes = archFor(opened.run_kind).notes;
  return notes === undefined ? nullMemory : recalling(notes(state, parentRunId));
}

export async function streamChat(state: State, request: ChatRequest, res: ServerResponse, build: HarnessFactory = harnessFor): Promise<void> {
  // Assembling the spec is inside the try because it is the likeliest thing to
  // refuse: the headers are already sent, so a refusal is a frame or it is nothing.
  try {
    const spec = chatSpec(request);
    const harness = build("chat" as RunKind, spec, state, await memoryFor(state, request.parent_run_id), FRESH, request.principal);
    const stream = runChat(harness, { run_id: request.run_id, spec, turns: request.turns });

    for (;;) {
      const next = await stream.next();
      if (next.done) break;
      for (const event of chatEvents(next.value)) res.write(sse(event));
      // The reader is gone, so the generator is finalised here rather than after
      // a whole answer nobody will read: its finally still journals the spend.
      if (res.writableEnded || res.destroyed) return void (await stream.return(undefined as never));
    }
  } catch (error) {
    log.error("chat turn failed", { run_id: request.run_id, ...errorFields(error) });
    res.write(sse({ error: error instanceof Error ? error.message : String(error) }));
  }
  res.end();
}

// The token alone, since ADR 0014, and the same trade Python's authorise makes --
// see core/agents/internal_auth.py. Both sides paired this with a loopback check
// until #635 made this process its own Deployment: the API then calls it from a pod
// address, which the check refused. The chart's NetworkPolicy names who may connect
// instead, which is what "same box" was standing in for.
//
// An unset token refuses everything, and that is now the only gate in the process.
//
// Over a digest rather than ===, which returns on the first differing byte.
// Hashed because timingSafeEqual throws on a length mismatch, and the throw would
// leak the length.
function authorised(req: IncomingMessage): boolean {
  const expected = process.env["AGENT_INTERNAL_TOKEN"] ?? process.env["VIGIL_TOOLS_TOKEN"] ?? "";
  if (expected === "") return false;
  const digest = (value: string): Buffer => createHash("sha256").update(value).digest();
  return timingSafeEqual(digest(req.headers.authorization ?? ""), digest(`Bearer ${expected}`));
}

async function body(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY) throw new SpecError(`a chat request may not exceed ${MAX_BODY} bytes`);
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function refuse(res: ServerResponse, status: number, detail: string): void {
  // A 4xx is the caller's doing and says nothing about this process; 5xx callers log first.
  if (status < 500) log.debug("request refused", { status });
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify({ detail }));
}

// A route that threw: logged with the run, then answered as the refusal it is.
function fail(res: ServerResponse, status: number, route: string, runId: string, error: unknown): void {
  log.error("request failed", { route, run_id: runId, status, ...errorFields(error) });
  refuse(res, status, error instanceof Error ? error.message : String(error));
}

// A run folded by the workflow that owns it. The events stay ours: a reader gets
// what the run decided or what it saw, never how either was written down.
async function foldedBy(state: State, runId: string, view: "projection" | "distil"): Promise<unknown | null> {
  const events = await state.read(runId);
  const opened = events[0];
  if (opened === undefined || !registeredKinds().includes(opened.run_kind)) return null;

  const project = archFor(opened.run_kind)[view];
  return project === undefined ? null : project(runId, events);
}

async function readFold(state: State, runId: string, view: "projection" | "distil", res: ServerResponse): Promise<void> {
  const folded = await foldedBy(state, runId, view);
  if (folded === null) return refuse(res, 404, `no readable run: ${runId}`);
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify(folded));
}

// A fresh account of a run, on demand. Served here rather than queued as a directive
// because it needs neither the lease nor the loop, which also makes it answerable for a
// run that has ended. The store assigns seq and the fold ignores the kind, so appending
// from outside does not break the ledger's one-writer rule.
async function writeNarrative(state: State, runId: string, res: ServerResponse, build: HarnessFactory): Promise<void> {
  const events = await state.read(runId);
  const opened = events[0];
  // Only a hunt-like ledger has this account. root_cause is not one, so it 404s
  // here. isHuntLike is the one gate, so a new hunt-like kind needs no edit here.
  if (opened === undefined || !isHuntLike(opened.run_kind)) return refuse(res, 404, `no hunt to write up: ${runId}`);

  // Narrowed on the line that established the kind: the store holds payloads as JSON and
  // never reads them. archFor() is the typed fix when a second kind wants an account.
  const hunt = state as unknown as State<HuntKinds>;
  const written = events as readonly HuntEvent[];

  try {
    const narrative = await narrateRun(hunt, runId, written, build);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(narrative));
  } catch (error) {
    return fail(res, 502, "narrate", runId, error);
  }
}

// What the hunt lead was shown at each decision, rebuilt from the ledger alone: no
// Memory, no verify, no append. An investigate run has no digest to rebuild, so it
// returns the journaled decisions and the calls that followed them. Compose and
// chat stay 404.
async function readReplay(state: State, runId: string, decisionId: string | null, res: ServerResponse): Promise<void> {
  const events = await state.read(runId);
  const opened = events[0];
  if (opened?.run_kind === "investigate") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(investigateReplay(runId, events)));
    return;
  }
  if (opened === undefined || !isHuntLike(opened.run_kind)) return refuse(res, 404, `no hunt to replay: ${runId}`);

  let report: ReplayReport;
  try {
    report = replay(events as readonly HuntEvent[]);
  } catch (error) {
    // Hunt-like is not the same as foldable: a ledger fold() refuses is a 502, as
    // writeNarrative answers, rather than a rejection nothing catches.
    return fail(res, 502, "replay", runId, error);
  }
  if (decisionId !== null) {
    const one = report.decisions.filter((decision) => decision.decision_id === decisionId);
    if (one.length === 0) return refuse(res, 404, `no such decision in ${runId}: ${decisionId}`);
    // Counts follow the filtered list; hunt_id and recalled are report-level and stay.
    report.decisions = one;
    report.reproduced = one.filter((decision) => decision.mismatch === null).length;
    report.inexact = one.filter((decision) => !decision.exact).length;
  }
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify(report));
}

async function readEvents(state: State, runId: string, snapshots: boolean, res: ServerResponse): Promise<void> {
  const events = await state.read(runId, snapshots ? { snapshots: true } : {});
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify({ events }));
}

async function readVerify(runId: string, verify: VerifyRun | undefined, res: ServerResponse): Promise<void> {
  if (verify === undefined) {
    log.error("ledger verify is not wired", { run_id: runId });
    return refuse(res, 500, "ledger verify is not wired");
  }
  try {
    const result = await verify(runId);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(result));
  } catch (error) {
    return fail(res, 502, "verify", runId, error);
  }
}

async function openChat(state: State, req: IncomingMessage, res: ServerResponse, build: HarnessFactory): Promise<void> {
  let request: ChatRequest;
  try {
    request = JSON.parse(await body(req)) as ChatRequest;
  } catch (error) {
    return refuse(res, 400, error instanceof Error ? error.message : String(error));
  }

  // Headers before the first token, so the reader is streaming rather than
  // buffering a response it will be handed all at once.
  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
  await streamChat(state, request, res, build);
}

export type VerifyRun = (runId: string) => Promise<VerifyResult>;

export function chatServer(state: State, ready: Ready, build: HarnessFactory = harnessFor, verify?: VerifyRun): Server {
  return createServer((req, res) => {
    void (async () => {
      // Before the auth check, because the kubelet has no token. These say only
      // whether the process can work, which is not knowledge worth withholding.
      if (await handleHealth(req, res, ready)) return;

      // Before the route, not per route: an unauthorised caller learns nothing
      // about which routes exist.
      if (!authorised(req)) return refuse(res, 401, "a valid internal token");

      const url = req.url ?? "";
      if (req.method === "POST" && url === CHAT) return openChat(state, req, res, build);

      const run = req.method === "GET" ? PROJECTION.exec(url) : null;
      if (run !== null) return readFold(state, run[1] as string, "projection", res);

      const distilled = req.method === "GET" ? DISTIL.exec(url) : null;
      if (distilled !== null) return readFold(state, distilled[1] as string, "distil", res);

      const asked = req.method === "POST" ? NARRATE.exec(url) : null;
      if (asked !== null) return writeNarrative(state, asked[1] as string, res, build);

      // Parsed rather than matched raw because this route takes a query. Node accepts
      // request-targets URL rejects (absolute-form with a bad host), so a throw here
      // is a 400 rather than an unhandled rejection.
      let parsed: URL;
      try {
        parsed = new URL(url, "http://local");
      } catch {
        return refuse(res, 400, `not a request path: ${url}`);
      }
      const replayed = req.method === "GET" ? REPLAY.exec(parsed.pathname) : null;
      if (replayed !== null) return readReplay(state, replayed[1] as string, parsed.searchParams.get("decision_id"), res);

      const verified = req.method === "GET" ? VERIFY.exec(parsed.pathname) : null;
      if (verified !== null) return readVerify(verified[1] as string, verify, res);

      const logged = req.method === "GET" ? EVENTS.exec(parsed.pathname) : null;
      if (logged !== null) return readEvents(state, logged[1] as string, parsed.searchParams.get("snapshots") === "1", res);

      return refuse(res, 404, `no such route: ${req.method} ${url}`);
    })();
  });
}

export function chatPort(): number {
  return Number(process.env["AGENT_HTTP_PORT"] ?? 6989);
}

// Whether this process can answer. Postgres and not Redis: serve reads and writes
// the ledger and never touches the queue, so queue connectivity would be reporting
// on something it does not use.
export function serveReady(pool: pg.Pool): Ready {
  return async () => {
    await pool.query("SELECT 1");
    return true;
  };
}

// The entry point `npm run serve` has always named and never had, so until #635 it
// loaded this module and exited. Its own pool: serve is a separate Deployment from
// the worker now, and a pool cannot be shared across processes.
if (process.argv[1] !== undefined && import.meta.url.endsWith(process.argv[1].split("/").pop() ?? "")) {
  const pool = new pg.Pool(poolConfig());
  // Cached: unauthenticated probes would otherwise take a connection each out of
  // the pool this process serves chat from.
  const serving = chatServer(new LedgerRepository(pool), cachedReady(serveReady(pool)), harnessFor, (runId) =>
    verifyLedger(pool, runId),
  ).listen(chatPort());
  const stop = () => {
    serving.close();
    void pool.end();
  };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}
