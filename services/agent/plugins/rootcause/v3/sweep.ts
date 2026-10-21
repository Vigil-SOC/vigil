// Cross-source sweep: when looking for an event's cause finds nothing, code (not the model) searches every
// other sourcetype for the event's identifying values over the whole data range. The survey says which sources
// exist; the sweep says which of them mention this event's accounts, roles, hosts, IPs and hashes, and when.
// The hits become leads on the node (with search ids the model can cite) and a boundary into the source that
// matched most values. The search per value is a presence lookup the coverage check reads too.
import type { Graph, GraphNode } from "./graph.js";
import type { LoopContext } from "./types.js";

const KINDS = new Set(["arn", "access-key", "principal", "user", "host", "ip", "file-hash", "domain-name", "url", "k8s-resource", "image-digest", "commit-sha", "workflow-run-id", "email-address", "message-id"]);
const MAX_VALUES = 6;

// The values worth sweeping: the node's artifacts, the accounts it ran as, and for ARNs their role/session names.
export function sweepValues(g: Graph, n: Pick<GraphNode, "id" | "artifacts">): string[] {
  const vals: string[] = [];
  const add = (v: string) => { const t = v.trim(); if (t.length >= 4 && !/^\d+$/.test(t) && !vals.some((x) => x.toLowerCase() === t.toLowerCase())) vals.push(t); };
  for (const a of n.artifacts) {
    if (!KINDS.has(a.kind)) continue;
    if (a.kind === "arn" || a.kind === "principal") a.value.split(/[:/]/).slice(-2).forEach(add);
    add(a.value);
  }
  for (const e of Object.values(g.edges)) if (e.target === n.id && e.kind === "attributed-to" && e.status !== "ruled-out") {
    const p = g.nodes[e.source]?.label ?? "";
    p.split(/[:/\\]/).slice(-2).forEach(add);
  }
  return vals.slice(0, MAX_VALUES);
}

export const quote = (v: string) => `"${v.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;

// Which sourcetypes hold events mentioning a value over the whole data range: one search per value per run (the
// tool registry is the run's), shared by the sweep and the coverage check (coverage.ts). The value is matched in an
// event's text or as its host, which a source may keep only in the index's metadata and never in the text.
// complete: false when the search failed or timed out, so the rows may leave out a source that holds the value.
export interface PresenceRow { sourcetype: string; count: string; first: string; last: string }
export interface Presence { toolCallId: string; rows: PresenceRow[]; complete: boolean }
const looked = new WeakMap<object, Map<string, Presence>>();
const COUNTS = /^[\d,]+ events matched.*$/m;

export async function presence(v: string, ctx: LoopContext, purpose: string, tag: Record<string, unknown>): Promise<Presence> {
  let seen = looked.get(ctx.tools);
  if (!seen) looked.set(ctx.tools, seen = new Map());
  const known = seen.get(v.toLowerCase());
  if (known) return known;
  const input = { purpose, spl: `index=${ctx.index} sourcetype=* (${quote(v)} OR host=${quote(v)}) | stats count min(_time) as first max(_time) as last by sourcetype`, earliest: ctx.range[0], latest: ctx.range[1] };
  const rec = await ctx.tools.call("splunk_query", input, { allowedDomains: ctx.domains, allDomains: ctx.domains, timeoutMs: ctx.cfg.toolTimeoutMs, range: ctx.range });
  ctx.trace.write("query", { toolCallId: rec.toolCallId, tool: "splunk_query", input, ...tag });
  ctx.trace.write("tool-output", { toolCallId: rec.toolCallId, ok: rec.result.ok, output: rec.result.ok ? rec.result.data : rec.result.error });
  const data = rec.result.ok ? rec.result.data ?? "" : "";
  const rows = data.split("\n").filter((l) => l.startsWith("{")).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean) as PresenceRow[];
  const counts = COUNTS.exec(data)?.[0];
  const p = { toolCallId: rec.toolCallId, rows, complete: !!counts && !/timed out/.test(counts) };
  if (rec.result.ok) seen.set(v.toLowerCase(), p); // a failed search is tried again when next asked
  return p;
}

export interface SweepResult { leads: string; topDomain?: string; hits: number }

export async function sweep(g: Graph, n: GraphNode, ctx: LoopContext): Promise<SweepResult> {
  const values = sweepValues(g, n);
  const byDomain = new Map<string, number>(); // sourcetype -> specificity-weighted count of values it matched
  const lines: string[] = [];
  for (const v of values) {
    const p = await presence(v, ctx, `cross-source sweep for ${n.id}: which sources mention ${v}`, { sweep: n.id });
    const rows = p.rows.filter((r) => r.sourcetype !== n.domain);
    const iso = (t: string) => new Date(Number(t) * 1000).toISOString().slice(0, 16).replace("T", " ");
    if (rows.length) {
      lines.push(`"${v}": ${rows.map((r) => `${r.sourcetype} ${r.count} events (${iso(r.first)} → ${iso(r.last)})`).join(", ")} [${p.toolCallId}]`);
      // A value seen in a few events of a source pins it more than one seen in hundreds.
      for (const r of rows) if (ctx.domains.includes(r.sourcetype)) byDomain.set(r.sourcetype, (byDomain.get(r.sourcetype) ?? 0) + Math.min(1, 10 / Math.max(1, Number(r.count))));
    } else lines.push(`"${v}": no other source [${p.toolCallId}]`);
  }
  const top = [...byDomain].sort((a, b) => b[1] - a[1])[0];
  return { leads: lines.join("; ") || "no identifying values to sweep", topDomain: top?.[0], hits: byDomain.size };
}
