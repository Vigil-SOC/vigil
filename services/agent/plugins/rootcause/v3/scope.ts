// Environment scoping: before the alert is shown, the model maps the environment from the data
// (systems, identities, connections, network, normal activity, names) with aggregate searches. The map is
// kept with the run and appended to the system prompt, so every later turn knows how the systems connect.
// A hand-written environment map, when the run holds one, is used instead.
import type { z } from "zod4";
import { constraints, coverageLine, instructions } from "./model/prompts.js";
import type { SubmitEnvironment } from "./model/schemas.js";
import type { AgentConfig } from "./config.js";
import type { Deps } from "./types.js";

export const SCOPE_SEARCHES = 14;
export const SCOPE_BATCH = 7; // two rounds: a first batch, then fixes for what came back empty
export const SCOPE_MIN_COUNT = 10;

export function renderEnvironment(e: z.infer<typeof SubmitEnvironment>): string {
  return [
    "## Your organisation's environment (mapped from the data before the investigation)",
    "How the systems connect and how often things happen (only activity seen at least 10 times). It says nothing about the incident; the logs decide what happened.",
    e.map.trim(),
  ].join("\n\n");
}

// Runs the scope turn (or reuses the map the run already holds, on resume) and hands the map to the model client.
export async function scope(deps: Deps, cfg: AgentConfig, range: [string, string], held?: string): Promise<string | null> {
  let text: string | null = held || null;
  if (!text) {
    const userContent = [instructions.scope(), constraints(cfg, range, deps.domains, deps.domains, coverageLine(deps.domains, {}, deps.sizes), SCOPE_SEARCHES)].join("\n\n");
    const r = await deps.model.runTurn({ move: "scope", correlationId: "environment", submitTool: "submit_environment", allowedDomains: deps.domains, maxSearches: SCOPE_SEARCHES, maxPerBatch: SCOPE_BATCH, minCount: SCOPE_MIN_COUNT, model: cfg.scopeModel, priceScale: cfg.scopePriceScale, trace: deps.trace.forTurn(0, undefined, "environment"), userContent });
    const sub = r.submission as z.infer<typeof SubmitEnvironment> | null;
    if (!sub) return null;
    text = renderEnvironment(sub);
  }
  deps.model.addContext?.(text);
  return text;
}
