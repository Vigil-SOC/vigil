// Shared types (spec §3.1, §13). A domain is a Splunk sourcetype, discovered at startup.
import type { AgentConfig } from "./config.js";
import type { ModelClient } from "./model/client.js";
import type { ToolRegistry } from "./tools/registry.js";
import type { TraceWriter, TurnTrace } from "./trace.js";
import type { Path } from "./graph.js";

export type Domain = string;

// What the run has spent so far, read from the pool that meters it.
export interface Spent { readonly usd: number }

export const ARTIFACT_KINDS = [
  "event-id", "process-guid", "parent-process-guid", "file-hash", "image-digest", "commit-sha", "workflow-run-id",
  "arn", "access-key", "k8s-resource", "cache-key", "pr-number", "url", "domain-name", "ip",
  "host", "user", "process", "principal",
] as const;
export type ArtifactKind = (typeof ARTIFACT_KINDS)[number];

// Strong: one value names one thing, so a shared value joins two records (dedup, direct stitch).
export const STRONG_KINDS: ReadonlySet<ArtifactKind> = new Set(["event-id", "process-guid", "file-hash", "image-digest", "commit-sha", "workflow-run-id"]);
// Medium: shared values make a link likely but not certain (corroborating stitch).
export const MEDIUM_KINDS: ReadonlySet<ArtifactKind> = new Set(["arn", "access-key", "k8s-resource", "cache-key", "pr-number", "url", "domain-name", "ip"]);

export interface Artifact {
  kind: ArtifactKind;
  value: string;
}

export type EvidenceStrength = "direct" | "corroborating" | "circumstantial";
export type EvidencePolarity = "supports" | "refutes";

export interface Evidence {
  id: string;
  source: Domain;
  summary: string;
  strength: EvidenceStrength;
  polarity: EvidencePolarity;
  toolCallIds: string[];
  traceIds: string[];
  turn: number;
  key?: string; // the same fact reported again (e.g. an identity value) counts once
}

export interface Alert {
  id: string;
  source: Domain;
  title: string;
  description?: string;
  observedAt: string;
  artifacts: Artifact[];
  raw: unknown;
}

export interface LoopContext {
  runId: string;
  turn: number;
  cfg: AgentConfig;
  model: ModelClient;
  tools: ToolRegistry;
  trace: TurnTrace;
  budget: Spent;
  domains: Domain[];
  sizes?: Record<Domain, number>; // events per sourcetype, from the survey
  range: [string, string];
  index: string;
  signal: AbortSignal;
}

export interface Deps {
  model: ModelClient;
  tools: ToolRegistry;
  trace: TraceWriter;
  budget: Spent;
  domains: Domain[];
  sizes?: Record<Domain, number>;
  clock: () => Date;
}

export type ExitReason = "root-cause-found" | "false-positive" | "inconclusive" | "budget-exhausted";

export interface RunResult {
  runId: string;
  exit: ExitReason;
  rootPath: Path | null;
  report: string; // report.md
}
