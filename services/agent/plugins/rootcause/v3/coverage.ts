// Coverage gaps: an empty search is evidence against an event only if the source searched records the event's
// entities at all. When every search a refuting item cites came back empty, code probes each sourcetype they
// read for the node's entity values over the whole data range; a source that holds none of them has a gap, and
// the item is dropped (a host with no Sysmon telemetry once took a confirmed alert from 0.50 to 0.03, and the run
// called it a false positive). A probe that fails or is unclear keeps the evidence: no gap is invented.
import type { Graph } from "./graph.js";
import { quote, sweepValues } from "./sweep.js";
import { searchedSourcetypes } from "./tools/registry.js";
import type { LoopContext } from "./types.js";

const EMPTY = /^0 events matched/m;
const MATCHED = /^([\d,]+) events matched(.*)$/m;

// Per run (the tool registry is the run's): what each probe found, and the gaps found for the model's next prompts.
interface RunState { probed: Map<string, boolean>; gaps: Map<string, Set<string>> }
const runs = new WeakMap<object, RunState>();
const stateOf = (ctx: LoopContext) => { let s = runs.get(ctx.tools); if (!s) runs.set(ctx.tools, s = { probed: new Map(), gaps: new Map() }); return s; };

// Does the sourcetype hold any event mentioning the value? undefined: the probe could not say.
async function records(st: string, value: string, ctx: LoopContext): Promise<boolean | undefined> {
  const s = stateOf(ctx), key = `${st}\0${value.toLowerCase()}`;
  if (s.probed.has(key)) return s.probed.get(key);
  const input = { purpose: `coverage probe: does ${st} record ${value} at all`, spl: `index=${ctx.index} sourcetype=${quote(st)} ${quote(value)} | stats count`, earliest: ctx.range[0], latest: ctx.range[1] };
  const rec = await ctx.tools.call("splunk_query", input, { allowedDomains: ctx.domains, allDomains: ctx.domains, timeoutMs: ctx.cfg.toolTimeoutMs, range: ctx.range });
  ctx.trace.write("query", { toolCallId: rec.toolCallId, tool: "splunk_query", input, coverageProbe: st });
  ctx.trace.write("tool-output", { toolCallId: rec.toolCallId, ok: rec.result.ok, output: rec.result.ok ? rec.result.data : rec.result.error });
  const m = rec.result.ok ? MATCHED.exec(rec.result.data ?? "") : null;
  if (!m || /timed out/.test(m[2]!)) return undefined;
  const found = Number(m[1]!.replace(/,/g, "")) > 0;
  s.probed.set(key, found);
  return found;
}

// The sourcetypes and entities of a gap when the cited searches all came back empty in sources that hold none
// of the entity values; undefined when the evidence stands.
export async function coverageGap(ctx: LoopContext, toolCallIds: string[], entities: string[]): Promise<{ sourcetypes: string[]; entities: string[] } | undefined> {
  const t = ctx.tools;
  if (!entities.length || !toolCallIds.length || !t.outputOf || !t.inputOf || !t.call) return undefined;
  if (!toolCallIds.every((id) => EMPTY.test(t.outputOf!(id) ?? ""))) return undefined;
  const sourcetypes = [...new Set(toolCallIds.flatMap((id) => searchedSourcetypes(t.inputOf!(id))))];
  if (!sourcetypes.length) return undefined;
  for (const st of sourcetypes) for (const v of entities) if ((await records(st, v, ctx)) !== false) return undefined;
  const gaps = stateOf(ctx).gaps;
  for (const st of sourcetypes) gaps.set(st, new Set([...(gaps.get(st) ?? []), ...entities]));
  return { sourcetypes, entities };
}

// The entity values of graph nodes or edges (an edge: both ends).
export function entitiesOf(g: Graph, ...ids: string[]): string[] {
  const nodes = ids.flatMap((id) => { const e = g.edges[id]; return e ? [g.nodes[e.source], g.nodes[e.target]] : [g.nodes[id]]; });
  return [...new Set(nodes.flatMap((n) => (n ? sweepValues(g, n) : [])))];
}

// One line for the model's next prompts: the sources found to hold nothing on an entity.
export function gapNote(ctx: LoopContext): string {
  const gaps = runs.get(ctx.tools)?.gaps;
  if (!gaps?.size) return "";
  return `Coverage gaps (checked by code): ${[...gaps].map(([st, es]) => `${st} holds no events on ${[...es].slice(0, 4).join(", ")}`).join("; ")}. An empty search there says nothing about whether the event happened; do not repeat it.`;
}
