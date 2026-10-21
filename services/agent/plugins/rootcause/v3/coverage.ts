// Coverage gaps: an empty search is evidence against an event only if the source searched records the event's
// entities at all. When every search a refuting item cites came back empty (0 events, 0 rows), code looks up each
// of the node's entity values over the whole data range (the sweep's presence lookup, one search per value per run);
// a sourcetype that holds none of them has a gap. The gap is per sourcetype: a cited search that read only such
// sources is dropped from the item, and the item is dropped when no cited search is left (a host with no Sysmon
// telemetry once took a confirmed alert from 0.50 to 0.03, and the run called it a false positive). A lookup that
// fails or times out keeps the evidence: no gap is invented.
import type { Graph } from "./graph.js";
import { presence, sweepValues } from "./sweep.js";
import { searchedSourcetypes } from "./tools/registry.js";
import type { LoopContext } from "./types.js";

const EMPTY = /^0 events matched, 0 results/m;

// Per run (the tool registry is the run's): the gaps found, for the model's next prompts.
const runs = new WeakMap<object, Map<string, Set<string>>>();

// Does the sourcetype hold any event on one of the values? undefined: a lookup could not say.
async function records(st: string, values: string[], ctx: LoopContext): Promise<boolean | undefined> {
  let unsure = false;
  for (const v of values) {
    const p = await presence(v, ctx, `coverage check: which sources record ${v} at all`, { coverage: v });
    if (p.rows.some((r) => r.sourcetype.toLowerCase() === st.toLowerCase() && Number(r.count) > 0)) return true;
    if (!p.complete) unsure = true;
  }
  return unsure ? undefined : false;
}

// When the cited searches all came back empty: the sourcetypes among them that hold none of the entity values, and
// the cited searches that still refute (they read a source that records the entity, or one that could not be
// checked). undefined when no cited source has a gap and the evidence stands whole.
export async function coverageGap(ctx: LoopContext, toolCallIds: string[], entities: string[]): Promise<{ sourcetypes: string[]; entities: string[]; standing: string[] } | undefined> {
  const t = ctx.tools;
  if (!entities.length || !toolCallIds.length || !t.outputOf || !t.inputOf || !t.call) return undefined;
  if (!toolCallIds.every((id) => EMPTY.test(t.outputOf!(id) ?? ""))) return undefined;
  const read = new Map(toolCallIds.map((id) => [id, searchedSourcetypes(t.inputOf!(id))]));
  const sourcetypes: string[] = [];
  for (const st of new Set([...read.values()].flat())) if ((await records(st, entities, ctx)) === false) sourcetypes.push(st);
  if (!sourcetypes.length) return undefined;
  let gaps = runs.get(t);
  if (!gaps) runs.set(t, gaps = new Map());
  for (const st of sourcetypes) gaps.set(st, new Set([...(gaps.get(st) ?? []), ...entities]));
  const standing = toolCallIds.filter((id) => !read.get(id)!.length || read.get(id)!.some((st) => !sourcetypes.includes(st)));
  return { sourcetypes, entities, standing };
}

// The entity values of graph nodes or edges (an edge: both ends).
export function entitiesOf(g: Graph, ...ids: string[]): string[] {
  const nodes = ids.flatMap((id) => { const e = g.edges[id]; return e ? [g.nodes[e.source], g.nodes[e.target]] : [g.nodes[id]]; });
  return [...new Set(nodes.flatMap((n) => (n ? sweepValues(g, n) : [])))];
}

// One line for the model's next prompts: the sources found to hold nothing on an entity.
export function gapNote(ctx: LoopContext): string {
  const gaps = runs.get(ctx.tools);
  if (!gaps?.size) return "";
  return `Coverage gaps (checked by code): ${[...gaps].map(([st, es]) => `${st} holds no events on ${[...es].slice(0, 4).join(", ")}`).join("; ")}. An empty search there says nothing about whether the event happened; do not repeat it.`;
}
