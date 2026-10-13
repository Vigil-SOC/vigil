import { randomUUID } from "node:crypto";
import { createServer, type Server } from "node:http";
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import OpenAI from "openai";
import { parse as parseYaml } from "yaml";
import { estateDispatch, loadCases, type DispatchLike, type EvalCase } from "./estate.js";
import { grade, keywordGrade, type Graded, type GraderClient } from "./grade.js";

// Runs the labelled cases through one engine, a result line per case:
// npx tsx eval/run.ts --engine <repo root> --label kernel --model anthropic/claude-sonnet-5-5

// The engine loads by path from the checkout named, each case one worker.advance() with tools
// from the case (estate.ts), no recall and prices.yaml; the model is the gateway's --model.

// --dry swaps in a model that answers by schema and calls nothing: wiring only, no measure.

const HERE = dirname(new URL(import.meta.url).pathname);

const { values: flags } = parseArgs({
  options: {
    engine: { type: "string" },
    label: { type: "string" },
    model: { type: "string", default: "anthropic/claude-sonnet-5-5" },
    "grader-model": { type: "string" },
    cases: { type: "string", default: join(HERE, "cases") },
    only: { type: "string" },
    out: { type: "string" },
    concurrency: { type: "string", default: "2" },
    dry: { type: "boolean", default: false },
  },
});

if (flags.engine === undefined || flags.label === undefined) {
  console.error("usage: tsx eval/run.ts --engine <repo root> --label <name> [--model provider/model] [--grader-model provider/model] [--only id,id] [--out file] [--concurrency n] [--dry]");
  process.exit(2);
}

const ENGINE = resolve(flags.engine, "services", "agent");
const LABEL = flags.label;
const MODEL = flags.dry ? "dry/schema-filler" : flags.model!;
const GRADER_MODEL = flags["grader-model"] ?? flags.model!;
const BIFROST = (process.env["BIFROST_URL"] ?? "http://127.0.0.1:8080").replace(/\/+$/, "");
const OUT = flags.out ?? join(HERE, "results", `${LABEL}.jsonl`);

// --- prices ------------------------------------------------------------------

type Card = { input: number; output: number; cache_read: number; cache_write: number };
const PRICES = parseYaml(readFileSync(join(HERE, "prices.yaml"), "utf8")) as Record<string, Card>;
const PER_MILLION = 1_000_000;

function cardFor(model: string, providerType = ""): Record<string, unknown> {
  const bare = model.includes("/") ? model.slice(model.lastIndexOf("/") + 1) : model;
  // A local model costs nothing, and says so, rather than reading as unpriced.
  const local = providerType === "ollama" || model.startsWith("ollama/");
  if (flags.dry || local) return { input: 0, output: 0, cache_read: 0, cache_write: 0, source: "zero", fetched_at: new Date().toISOString() };
  const card = PRICES[bare];
  if (card === undefined) return { source: "unknown" };
  return {
    input: card.input / PER_MILLION,
    output: card.output / PER_MILLION,
    cache_read: card.cache_read / PER_MILLION,
    cache_write: card.cache_write / PER_MILLION,
    source: "eval/prices.yaml",
    fetched_at: new Date().toISOString(),
  };
}

// The engine asks its pricing catalogue over HTTP; this answers in its place.
async function priceServer(): Promise<Server> {
  const server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://localhost");
    res.writeHead(200, { "content-type": "application/json" });
    // The same base serves the gateway's virtual key in production; the eval sets none.
    if (url.pathname.endsWith("/vk")) {
      res.end(JSON.stringify({ vk: null }));
      return;
    }
    res.end(JSON.stringify(cardFor(url.searchParams.get("model_id") ?? "", url.searchParams.get("provider_type") ?? "")));
  });
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("the price server did not bind");
  process.env["VIGIL_PRICING_URL"] = `http://127.0.0.1:${address.port}/internal/pricing`;
  return server;
}

// --- the dry model -------------------------------------------------------------

// Fills a schema with the first value it allows. Asked for an answer, it gives the
// least committal one it can; asked to act, it calls nothing.
function fill(schema: Record<string, unknown>): unknown {
  const kind = schema["type"];
  const options = schema["enum"];
  if (Array.isArray(options)) {
    const preferred = ["CONCLUDE", "inconclusive", "insufficient"].find((one) => options.includes(one));
    return preferred ?? options[0];
  }
  if (kind === "object") {
    const properties = (schema["properties"] ?? {}) as Record<string, Record<string, unknown>>;
    const required = (schema["required"] ?? []) as string[];
    return Object.fromEntries(required.map((key) => [key, fill(properties[key] ?? { type: "string" })]));
  }
  if (kind === "array") return [];
  if (kind === "number" || kind === "integer") return 0;
  if (kind === "boolean") return false;
  return "dry run: nothing was examined, so this is inconclusive";
}

const ZERO = { input: 0, output: 0, cache_read: 0, cache_write: 0 };

// Reads the finding once, so the estate is exercised, then stops calling tools.
const dryProvider = {
  model: MODEL,
  provider_type: "dry",
  stream: async function* (request: { emit?: Record<string, unknown>; tools: readonly { id: string }[]; messages: readonly { role: string }[] }) {
    if (request.emit !== undefined) yield { type: "text_delta" as const, text: JSON.stringify(fill(request.emit)) };
    yield { type: "usage" as const, tokens: ZERO };
    const looked = request.messages.some((message) => message.role === "tool");
    if (request.emit === undefined && !looked && request.tools.some((tool) => tool.id === "get_finding")) {
      yield { type: "tool_call" as const, call: { id: "dry-1", tool: "get_finding", args: '{"finding_id":"f-dry"}' } };
    }
  },
};

// --- the grader ----------------------------------------------------------------

function graderClient(): GraderClient {
  const client = new OpenAI({ baseURL: `${BIFROST}/v1`, apiKey: process.env["BIFROST_API_KEY"] ?? "unused", maxRetries: 2 });
  const card = cardFor(GRADER_MODEL);
  return {
    model: GRADER_MODEL,
    complete: async (system, user) => {
      const response = await client.chat.completions.create({
        model: GRADER_MODEL,
        // Room for a model that reasons before it answers: a thinking model given a
        // small ceiling spends it all thinking and returns no text.
        max_tokens: 2_000,
        messages: [
          { role: "system", content: system },
          { role: "user", content: user },
        ],
      });
      const usage = response.usage;
      const cost =
        usage === undefined || typeof card["input"] !== "number"
          ? undefined
          : usage.prompt_tokens * (card["input"] as number) + usage.completion_tokens * (card["output"] as number);
      return { text: response.choices[0]?.message?.content ?? "", ...(cost === undefined ? {} : { cost_usd: cost }) };
    },
  };
}

// --- one case ------------------------------------------------------------------

interface Engine {
  advance: (state: unknown, leases: unknown, job: unknown, build: unknown) => Promise<void>;
  harnessFor: (...args: unknown[]) => Record<string, unknown> & { dispatch: DispatchLike };
  InProcessState: new () => { read(runId: string): Promise<{ kind: string; payload: unknown }[]> };
  InProcessLeases: new () => unknown;
  nullMemory: unknown;
  commit: string;
}

async function loadEngine(): Promise<Engine> {
  const at = (path: string) => import(pathToFileURL(join(ENGINE, path)).href);
  const [worker, harness, state, leases, memory] = await Promise.all([at("worker.ts"), at("harness.ts"), at("core/state.ts"), at("core/leases.ts"), at("core/memory.ts")]);
  const { execSync } = await import("node:child_process");
  const commit = execSync("git rev-parse --short HEAD", { cwd: ENGINE }).toString().trim();
  return {
    advance: worker.advance,
    harnessFor: harness.harnessFor,
    InProcessState: state.InProcessState,
    InProcessLeases: leases.InProcessLeases,
    nullMemory: memory.nullMemory,
    commit,
  };
}

function configFor(dir: string): string {
  const template = readFileSync(join(HERE, "investigate.config.yaml"), "utf8");
  const path = join(dir, "investigate.config.yaml");
  writeFileSync(path, template.replace(/^model: MODEL$/m, `model: ${MODEL}`));
  return path;
}

export interface CaseResult {
  label: string;
  engine_commit: string;
  model: string;
  case_id: string;
  truth: string;
  traps: string[];
  verdict: Graded["verdict"] | null;
  grade_reason: string;
  // The kernel's own structured answer, when the engine gave one.
  self_disposition: string | null;
  // Set when a guard changed the answer: what was asked, and what stood.
  guarded: { asked: string; final: string } | null;
  outcome: string | null;
  report: string;
  cost_usd: number;
  unpriced_calls: number;
  model_calls: number;
  // Summed off the run's spend events, which carry what the provider reported.
  tokens: { input: number; output: number; cache_read: number; cache_write: number };
  tool_calls: number;
  blind_calls: number;
  wall_ms: number;
  error: string | null;
}

async function runCase(engine: Engine, one: EvalCase, config: string, grader: GraderClient | null): Promise<CaseResult> {
  const state = new engine.InProcessState();
  const leases = new engine.InProcessLeases();
  const runId = randomUUID();
  const calls: { tool: string; args: string }[] = [];
  const build = (...args: unknown[]) => {
    const [kind, spec, held, , seed, principal] = args;
    const harness = engine.harnessFor(kind, spec, held, engine.nullMemory, seed, principal);
    return { ...harness, memory: engine.nullMemory, dispatch: estateDispatch(one, harness.dispatch, calls), ...(flags.dry ? { provider: dryProvider } : {}) };
  };
  const job = {
    schema_version: 1,
    run_id: runId,
    run_kind: "investigate",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "eval",
    reason: "start",
    request: { arch: "", playbook: join(HERE, "investigate.playbook.yaml"), config, prompt: one.alert },
  };

  const started = Date.now();
  let error: string | null = null;
  try {
    await engine.advance(state, leases, job, build);
  } catch (caught) {
    error = caught instanceof Error ? caught.message : String(caught);
  }
  const wall_ms = Date.now() - started;
  const events = await state.read(runId);
  // Kept, so where a case's calls went can be read afterwards.
  const ledgers = join(dirname(OUT), "ledgers", LABEL);
  mkdirSync(ledgers, { recursive: true });
  writeFileSync(join(ledgers, `${one.id}.jsonl`), events.map((event) => JSON.stringify(event)).join("\n") + "\n");

  const terminal = events.findLast((event) => event.kind === "terminal")?.payload as { outcome?: string; reason?: string; summary?: string } | undefined;
  const conclusion = events.findLast((event) => event.kind === "conclusion")?.payload as { value?: Record<string, unknown> } | undefined;
  const review = events.findLast((event) => event.kind === "review")?.payload as { guarded?: { asked: string; final: string } } | undefined;
  const spends = events
    .filter((event) => event.kind === "spend")
    .map((event) => event.payload as { cost_usd: number | null; tokens?: Partial<Record<"input" | "output" | "cache_read" | "cache_write", number>> });
  const tokens = { input: 0, output: 0, cache_read: 0, cache_write: 0 };
  for (const spend of spends) for (const key of Object.keys(tokens) as (keyof typeof tokens)[]) tokens[key] += spend.tokens?.[key] ?? 0;
  const report = terminal?.summary ?? terminal?.reason ?? "";

  let graded: Graded | null = null;
  if (terminal !== undefined) {
    try {
      graded = grader === null ? keywordGrade(report) : await grade(grader, report);
    } catch (caught) {
      error = error ?? `grading failed: ${caught instanceof Error ? caught.message : String(caught)}`;
    }
  } else {
    error = error ?? "the run reached no terminal";
  }

  const disposition = conclusion?.value?.["disposition"];
  return {
    label: LABEL,
    engine_commit: engine.commit,
    model: MODEL,
    case_id: one.id,
    truth: one.truth,
    traps: one.traps,
    verdict: graded?.verdict ?? null,
    grade_reason: graded?.reason ?? "",
    self_disposition: typeof disposition === "string" ? disposition : null,
    guarded: review?.guarded === undefined ? null : { asked: review.guarded.asked, final: review.guarded.final },
    outcome: terminal?.outcome ?? null,
    report,
    cost_usd: Number(spends.reduce((total, spend) => total + (spend.cost_usd ?? 0), 0).toFixed(6)),
    unpriced_calls: spends.filter((spend) => spend.cost_usd === null).length,
    model_calls: spends.length,
    tokens,
    tool_calls: calls.length,
    blind_calls: calls.filter((call) => {
      const response = one.tools[call.tool];
      return response !== undefined && response.some((candidate) => candidate.failure !== undefined && (candidate.match ?? []).every((term) => call.args.toLowerCase().includes(term.toLowerCase())));
    }).length,
    wall_ms,
    error,
  };
}

// --- main ----------------------------------------------------------------------

async function main(): Promise<void> {
  const prices = await priceServer();
  process.env["BIFROST_URL"] = BIFROST;
  const engine = await loadEngine();
  const wanted = flags.only?.split(",").map((one) => one.trim());
  const cases = loadCases(flags.cases!).filter((one) => wanted === undefined || wanted.includes(one.id));
  const work = join(tmpdir(), `vigil-eval-${LABEL}-${process.pid}`);
  mkdirSync(work, { recursive: true });
  mkdirSync(dirname(OUT), { recursive: true });
  const config = configFor(work);
  const grader = flags.dry ? null : graderClient();

  console.log(`${LABEL}: ${cases.length} cases on ${engine.commit} with ${MODEL}, graded by ${grader?.model ?? "keywords"}`);
  const queue = [...cases];
  const width = Math.max(1, Number(flags.concurrency));
  await Promise.all(
    Array.from({ length: width }, async () => {
      for (let one = queue.shift(); one !== undefined; one = queue.shift()) {
        const result = await runCase(engine, one, config, grader);
        appendFileSync(OUT, `${JSON.stringify(result)}\n`);
        const mark = result.error !== null ? "ERROR" : result.verdict ?? "-";
        console.log(`  ${one.id.padEnd(34)} truth=${one.truth.padEnd(6)} got=${String(mark).padEnd(12)} $${result.cost_usd.toFixed(4)} ${result.model_calls} model / ${result.tool_calls} tool calls${result.error ? `  (${result.error.slice(0, 120)})` : ""}`);
      }
    }),
  );
  prices.close();
  console.log(`wrote ${OUT}`);
}

await main();
