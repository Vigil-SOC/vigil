import { startWorker } from "../../worker.js";
import { isLead, respondingProvider } from "./responding-provider.js";
import { budgetOf, FRESH, unmeteredQuota } from "../../core/budget.js";
import { localDispatch } from "../../core/dispatch.js";
import { nullMemory } from "../../core/memory.js";
import { registryOf } from "../../core/registry.js";
import type { HarnessFactory } from "../../harness.js";

// Drains one job and exits, so an integration test can spawn the worker without
// owning a long-lived process. Not a deployment entrypoint; that is worker.ts.
const DRAIN_TIMEOUT_MS = 30_000;

// The model is the only part stood in for: the queue, the ledger, the leases and
// the API either side are real, and they are what the seam is made of.

// The two arches name their citation field differently and both refuse an unknown
// property, so which is sent is read off the schema rather than guessed.
function decision(schema: Record<string, unknown>): unknown {
  const properties = (schema["properties"] ?? {}) as Record<string, unknown>;
  const cites = "citations" in properties ? "citations" : "evidence_citations";
  return { action: "CONCLUDE", rationale: "the skeleton concludes", [cites]: [] };
}

// Any other answer the run asks for, such as the narrator's account, gets the first
// value its schema allows, so the run ends on what it was asked for.
function fill(schema: Record<string, unknown>): unknown {
  const options = schema["enum"];
  if (Array.isArray(options)) return options[0];
  const kind = schema["type"];
  if (kind === "object") {
    const properties = (schema["properties"] ?? {}) as Record<string, Record<string, unknown>>;
    const required = (schema["required"] ?? []) as string[];
    return Object.fromEntries(required.map((key) => [key, fill(properties[key] ?? { type: "string" })]));
  }
  if (kind === "array") return [];
  if (kind === "number" || kind === "integer") return 0;
  if (kind === "boolean") return false;
  return "the skeleton concludes";
}

const provider = respondingProvider({
  emit: (schema) => (isLead(schema) ? decision(schema) : fill(schema)),
  ticks: 0,
});

const build: HarnessFactory = (_kind, spec, state, memory = nullMemory, seed = FRESH) =>
  ({
    provider,
    registry: registryOf([], {}),
    dispatch: localDispatch,
    budget: budgetOf(spec.budgets, unmeteredQuota, Date.now, seed),
    memory,
    state,
  }) as never;

const { worker, close } = startWorker(build);
const stop = async (code: number) => {
  await close();
  process.exit(code);
};

worker.on("completed", (job) => {
  console.log(`completed ${job.id}`);
  void stop(0);
});
worker.on("failed", (job, error) => {
  console.error(`failed ${job?.id}: ${error.message}`);
  void stop(1);
});

setTimeout(() => {
  console.error("no job consumed before the drain timeout");
  void stop(2);
}, DRAIN_TIMEOUT_MS);
