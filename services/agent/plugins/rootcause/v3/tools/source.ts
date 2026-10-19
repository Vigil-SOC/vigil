import type { ToolResult } from "../../../../contracts/tool.js";
import type { Survey } from "../survey.js";
import type { EvidenceTool } from "./registry.js";

// Where a run's telemetry lives, whichever SIEM holds it. The loop never sees a backend:
// it searches through one evidence tool, scoped to this run's data and window and
// read-only, and learns what it can search from the survey. An adapter per backend turns
// the bound telemetry tool into both, in that backend's own query language.

export interface Source {
  // The bound server, as the capability binding names it: "splunk-selfhosted", "elastic".
  readonly backend: string;
  // What the model writes a search in: "spl" for Splunk. The prompts name it.
  readonly language: string;
  // The searches the loop runs: guarded, windowed, capped, counted first when raw.
  evidenceTool(): EvidenceTool;
  // The tables a search can name (a domain each), their sizes, spans and fields.
  survey(): Promise<Survey>;
}

// One call of the run's bound telemetry tool, through the agent's dispatch, so it is
// bounded, attributed and journaled like any other tool call.
export type Invoke = (args: Record<string, unknown>) => Promise<ToolResult>;

// The data a run searches: one index (or its equivalent) over the incident's span.
export interface SourceScope {
  index: string;
  range: [string, string];
}

export interface SourceAdapter {
  backend: string;
  // The tool ids, as the binding flattens them, this adapter knows how to call.
  tools: readonly string[];
  open(invoke: Invoke, scope: SourceScope): Source;
}

export class UnsupportedSource extends Error {}

// The adapter for whichever telemetry tool the run was bound to, the first that knows one.
// A run whose binding no adapter reads is refused, never sent to search blind.
export function sourceFor(adapters: readonly SourceAdapter[], bound: readonly string[]): { adapter: SourceAdapter; tool: string } {
  for (const adapter of adapters) {
    const tool = bound.find((id) => adapter.tools.includes(id));
    if (tool !== undefined) return { adapter, tool };
  }
  throw new UnsupportedSource(
    `root cause reads ${adapters.map((a) => a.backend).join(", ")} so far, and this run's telemetry is ${bound.length ? bound.join(", ") : "not bound"}`,
  );
}
