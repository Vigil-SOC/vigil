// Compact graph snapshot (spec §5.6): line-oriented, ordered by id. Best path and its neighbors, every live
// event (branches too, up to MAX_NODES), every open marker, every disputed edge; ruled-out hypotheses one line
// each so they are not re-proposed. Evidence lines carry their search ids so later turns can cite them.
import { allPaths, type Graph, type GraphNode } from "../graph.js";

const byId = (a: string, b: string) => a.replace(/\d+$/, "").localeCompare(b.replace(/\d+$/, "")) || Number(a.match(/\d+$/)?.[0]) - Number(b.match(/\d+$/)?.[0]);
const KIND: Record<GraphNode["kind"], string> = { action: "action", actor: "actor", "boundary-marker": "boundary" };
const hhmm = (s?: string) => (s ? ` @${s.replace(/:\d\d(\.\d+)?Z$/, "Z")}` : "");

const MAX_NODES = 60;

export function snapshot(g: Graph, maxEvidence = 2): string {
  const best = allPaths(g, 1)[0];
  const include = new Set<string>(best?.nodeIds ?? []);
  const edgeIds = new Set<string>(best?.edgeIds ?? []);
  for (const e of Object.values(g.edges)) {
    if (e.retired) continue;
    if (include.has(e.source) || include.has(e.target)) edgeIds.add(e.id);
    if (e.disputed) edgeIds.add(e.id);
  }
  for (const n of Object.values(g.nodes)) if (n.kind === "boundary-marker" && !n.resolvedTo) include.add(n.id);
  for (const n of Object.values(g.nodes)) if (n.kind === "action" && n.status !== "ruled-out" && include.size < MAX_NODES) include.add(n.id);
  for (const e of Object.values(g.edges)) if (!e.retired && e.kind === "explains" && include.has(e.source) && include.has(e.target)) edgeIds.add(e.id);
  for (const id of edgeIds) { include.add(g.edges[id].source); include.add(g.edges[id].target); }

  const live = [...include].filter((id) => g.nodes[id].status !== "ruled-out").sort(byId);
  const lines = ["NODES"];
  for (const id of live) {
    const n = g.nodes[id];
    const conf = n.kind === "boundary-marker" ? "-" : n.confidence.toFixed(2);
    const tags = [n.isSymptom ? "SYMPTOM" : "", n.isRoot ? "ROOT" : "", n.legitimate ? "LEGITIMATE" : "", n.unverified ? "UNVERIFIED" : "", best?.nodeIds.includes(id) ? "PATH" : ""].filter(Boolean).join(" ");
    const label = n.kind === "boundary-marker" ? `upstream cause of ${n.originNodeId} in ${n.targetDomain} (${n.reason ?? ""})` : n.label;
    lines.push(`${n.id} [${KIND[n.kind]}|${n.status}|${conf}|${n.domain}] ${label}${hhmm(n.observedAt)}${tags ? ` ${tags}` : ""}`);
    if (n.artifacts.length && n.kind !== "actor") lines.push(`   artifacts: ${n.artifacts.slice(0, 8).map((a) => `${a.kind}=${a.value}`).join(", ")}`);
    for (const e of n.evidence.slice(-maxEvidence)) lines.push(`   · ${e.polarity === "refutes" ? "−" : "+"}${e.strength}${e.toolCallIds.length ? ` [${e.toolCallIds.join(",")}]` : ""}: ${e.summary.slice(0, 160)}`);
  }
  lines.push("EDGES");
  for (const id of [...edgeIds].sort(byId)) {
    const e = g.edges[id];
    if (e.status === "ruled-out") continue;
    lines.push(`${e.id} ${e.source} -${e.kind}-> ${e.target} ${e.confidence.toFixed(2)}${e.disputed ? " DISPUTED" : ""}${e.joint ? " JOINT" : ""}${e.inferred ? " INFERRED" : ""}${e.status === "confirmed" ? " confirmed" : ""}`);
  }
  const out = Object.values(g.nodes).filter((n) => n.status === "ruled-out").sort((a, b) => byId(a.id, b.id));
  const outEdges = Object.values(g.edges).filter((e) => e.status === "ruled-out" && !e.retired).sort((a, b) => byId(a.id, b.id));
  if (out.length || outEdges.length) {
    lines.push("RULED OUT");
    for (const n of out) lines.push(`${n.id} ${n.label} (${n.evidence.find((e) => e.polarity === "refutes")?.summary.slice(0, 120) ?? "no support"})`);
    for (const e of outEdges) lines.push(`${e.id} ${g.nodes[e.source].label} -${e.kind}-> ${e.target}`);
  }
  const th = g.theories ?? [];
  if (th.length) {
    lines.push("STORIES (competing explanations of the whole incident; latest round live)");
    for (const t of th.filter((x) => x.status === "live")) {
      lines.push(`${t.id} [+${t.support} −${t.against}] ${t.story}`, `   gaps: ${t.gaps}`);
      for (const p of t.predictions) lines.push(`   ${p.id} [${p.status}] ${p.sourcetype}: ${p.expect.slice(0, 160)}${p.result ? ` → ${p.result.slice(0, 200)}${p.toolCallIds?.length ? ` [${p.toolCallIds.join(",")}]` : ""}` : ""}`);
    }
    const old = th.filter((x) => x.status === "replaced").flatMap((t) => t.predictions.filter((p) => p.status !== "open").map((p) => `   ${p.id} (${t.id}) [${p.status}] ${p.expect.slice(0, 100)} → ${(p.result ?? "").slice(0, 140)}`));
    if (old.length) lines.push("EARLIER TESTS", ...old);
  }
  return `<graph_snapshot>\n${lines.join("\n")}\n</graph_snapshot>`;
}
