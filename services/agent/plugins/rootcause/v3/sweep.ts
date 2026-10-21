// Cross-source sweep: when looking for an event's cause finds nothing, code (not the model) searches every
// other sourcetype for the event's identifying values over the whole data range. The survey says which sources
// exist; the sweep says which of them mention this event's accounts, roles, hosts, IPs and hashes, and when.
// The hits become leads on the node (with search ids the model can cite) and a boundary into the source that
// matched most values.
import type { Graph, GraphNode } from "./graph.js";
import type { LoopContext } from "./types.js";

const KINDS = new Set(["arn", "access-key", "principal", "user", "host", "ip", "file-hash", "domain-name", "url", "k8s-resource", "image-digest", "commit-sha", "workflow-run-id", "email-address", "message-id"]);
const MAX_VALUES = 6;

// The values worth sweeping: the node's artifacts, the accounts it ran as, and for ARNs their role/session names.
export function sweepValues(g: Graph, n: GraphNode): string[] {
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

const quote = (v: string) => `"${v.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;

export interface SweepResult { leads: string; topDomain?: string; hits: number }

export async function sweep(g: Graph, n: GraphNode, ctx: LoopContext): Promise<SweepResult> {
  const values = sweepValues(g, n);
  const byDomain = new Map<string, number>(); // sourcetype -> specificity-weighted count of values it matched
  const lines: string[] = [];
  for (const v of values) {
    const input = { purpose: `cross-source sweep for ${n.id}: which other sources mention ${v}`, spl: `index=${ctx.index} NOT sourcetype=${quote(n.domain)} ${quote(v)} | stats count min(_time) as first max(_time) as last by sourcetype`, earliest: ctx.range[0], latest: ctx.range[1] };
    const rec = await ctx.tools.call("splunk_query", input, { allowedDomains: ctx.domains, allDomains: ctx.domains, timeoutMs: ctx.cfg.toolTimeoutMs, range: ctx.range });
    ctx.trace.write("query", { toolCallId: rec.toolCallId, tool: "splunk_query", input, sweep: n.id });
    ctx.trace.write("tool-output", { toolCallId: rec.toolCallId, ok: rec.result.ok, output: rec.result.ok ? rec.result.data : rec.result.error });
    const rows = (rec.result.data ?? "").split("\n").filter((l) => l.startsWith("{")).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean) as { sourcetype: string; count: string; first: string; last: string }[];
    const iso = (t: string) => new Date(Number(t) * 1000).toISOString().slice(0, 16).replace("T", " ");
    if (rows.length) {
      lines.push(`"${v}": ${rows.map((r) => `${r.sourcetype} ${r.count} events (${iso(r.first)} → ${iso(r.last)})`).join(", ")} [${rec.toolCallId}]`);
      // A value seen in a few events of a source pins it more than one seen in hundreds.
      for (const r of rows) if (ctx.domains.includes(r.sourcetype)) byDomain.set(r.sourcetype, (byDomain.get(r.sourcetype) ?? 0) + Math.min(1, 10 / Math.max(1, Number(r.count))));
    } else lines.push(`"${v}": no other source [${rec.toolCallId}]`);
  }
  const top = [...byDomain].sort((a, b) => b[1] - a[1])[0];
  return { leads: lines.join("; ") || "no identifying values to sweep", topDomain: top?.[0], hits: byDomain.size };
}
