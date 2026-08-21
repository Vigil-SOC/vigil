import { createHash, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import pg from "pg";
import { archFor, registeredKinds } from "./arch/registry.js";
import { cachedReady, handleHealth, type Ready } from "./core/health.js";
import { LedgerRepository } from "./ledger/repository.js";
import { poolConfig } from "./core/db.js";
import type { RunKind } from "./contracts/events.js";
import { nullMemory, recalling } from "./core/memory.js";
import type { Memory, State } from "./core/seams.js";
import { assembleSpec, loadArch, parseConfig, parsePlaybook, SpecError, type Playbook, type RunSpec } from "./core/spec.js";
import { chatEvents, sse } from "./workflows/chat/sse.js";
import { runChat, type Turn } from "./workflows/chat/workflow.js";
import { harnessFor, type HarnessFactory } from "./harness.js";
import { narrateRun } from "./workflows/hunt/workflow.js";
import type { HuntEvent, HuntKinds } from "./workflows/hunt/ledger.js";

const CHAT = "/chat/stream";
// GET /runs/<id>/projection -- what a supervisor outside this process reads.
const PROJECTION = /^\/runs\/([0-9a-fA-F-]{36})\/projection$/;
const NARRATE = /^\/runs\/([0-9a-fA-F-]{36})\/narrate$/;
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
    const harness = build("chat" as RunKind, spec, state, await memoryFor(state, request.parent_run_id));
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
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify({ detail }));
}

// A run folded by the workflow that owns it. The events stay ours: a supervisor
// reads what the run decided, never how it was written down.
export async function projectionOf(state: State, runId: string): Promise<unknown | null> {
  const events = await state.read(runId);
  const opened = events[0];
  if (opened === undefined || !registeredKinds().includes(opened.run_kind)) return null;

  const project = archFor(opened.run_kind).projection;
  return project === undefined ? null : project(runId, events);
}

async function readProjection(state: State, runId: string, res: ServerResponse): Promise<void> {
  const projection = await projectionOf(state, runId);
  if (projection === null) return refuse(res, 404, `no readable run: ${runId}`);
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify(projection));
}

// A fresh account of a run, on demand. Served here rather than queued as a directive
// because it needs neither the lease nor the loop, which also makes it answerable for a
// run that has ended. The store assigns seq and the fold ignores the kind, so appending
// from outside does not break the ledger's one-writer rule.
async function writeNarrative(state: State, runId: string, res: ServerResponse, build: HarnessFactory): Promise<void> {
  const events = await state.read(runId);
  const opened = events[0];
  if (opened === undefined || opened.run_kind !== "hunt") return refuse(res, 404, `no hunt to write up: ${runId}`);

  try {
    const narrative = await narrateRun(state as unknown as State<HuntKinds>, runId, events as readonly HuntEvent[], build);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(narrative));
  } catch (error) {
    return refuse(res, 502, error instanceof Error ? error.message : String(error));
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

export function chatServer(state: State, ready: Ready, build: HarnessFactory = harnessFor): Server {
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
      if (run !== null) return readProjection(state, run[1] as string, res);

      const asked = req.method === "POST" ? NARRATE.exec(url) : null;
      if (asked !== null) return writeNarrative(state, asked[1] as string, res, build);

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
  const serving = chatServer(new LedgerRepository(pool), cachedReady(serveReady(pool))).listen(chatPort());
  const stop = () => {
    serving.close();
    void pool.end();
  };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}
