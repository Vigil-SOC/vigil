import { key } from "./entities.js";
import type { Enricher } from "./ports.js";
import { SpecError } from "../../core/spec.js";
import type { EnrichmentChain, HuntSpec } from "./config.js";
export interface Tool {
  id: string;
  description: string;
  parameters: Record<string, unknown>;
  run(args: Record<string, unknown>): Promise<string>;
  close?(): Promise<void>;
}
import type { Entity, WorkerEvidence } from "./types.js";

// The value is telemetry an adversary wrote and it is going into SQL. The
// connection is read-only and assertReadOnly rejects a second statement, so the
const UNSAFE = /["`\x00-\x1F\x7F]/;
const MAX_VALUE = 256;

export function templatable(value: string): boolean {
  return value.length > 0 && value.length <= MAX_VALUE && !UNSAFE.test(value);
}

function literal(value: string): string {
  return value.replaceAll("'", "''");
}

async function run(chain: EnrichmentChain, entity: Entity, tool: Tool): Promise<WorkerEvidence> {
  const entityKey = key(entity);
  let result: string;
  try {
    result = await tool.run({ sql: chain.query.replaceAll("{{value}}", literal(entity.value)) });
  } catch (error) {
    result = `enrichment query failed: ${(error as Error).message}`;
  }

  return {
    source_system: chain.id,
    summary: `${chain.id} on ${entityKey}: ${result}`,
    // entity is read back as the dedup key, so a chain never re-runs on a value
    // the ledger already covers, including across a resume.
    payload: { chain: chain.id, entity: entityKey, result },
    salience: "routine",
    why_notable: `deterministic ${chain.id} enrichment; no one chose to run it`,
    provenance: `enrichment:${chain.id}`,
    // Raw telemetry an adversary may have authored, so an enriched signing cert
    // can never on its own be the grounds for an ABANDON.
    attacker_influenceable: true,
    instruction_like: false,
  };
}

// Runs every chain that applies to the entity, so the controller can dedup on the
// entity alone: once enriched, all of its chains have already run.
export function createEnricher(spec: HuntSpec, tools: readonly Tool[]): Enricher | undefined {
  const chains = spec.enrichment.chains;
  if (chains.length === 0) return undefined;

  const bound = chains.map((chain) => {
    const tool = tools.find((candidate) => candidate.id === chain.tool);
    if (tool === undefined) throw new SpecError(`enrichment chain ${chain.id} needs tool ${chain.tool}, which was not built`);
    return { chain, tool };
  });

  return async (entity) => {
    if (!templatable(entity.value)) return [];
    return Promise.all(
      bound.filter(({ chain }) => chain.on === entity.type).map(({ chain, tool }) => run(chain, entity, tool)),
    );
  };
}
