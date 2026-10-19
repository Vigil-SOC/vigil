// Model-led moves (v3-temp): each submission may name the next move (`next`); code only checks it is one of
// the moves available on the graph now. Unavailable or missing, the frontier picks as before.
import type { AgentConfig } from "./config.js";
import { isStalled, pivotExhausted, toConnect, type FrontierDecision, type Move } from "./frontier.js";
import { bestPathFromSymptom, getParents, type Graph, type GraphNode, type Prediction } from "./graph.js";
import { mentionsRare, rareValues, type RareValue } from "./rare.js";

export interface NextIn { move: string; target?: string; source?: string; why?: string }
export interface LeadState { next?: NextIn; refused?: string }

const live = (n: GraphNode) => n.kind === "action" && n.status !== "ruled-out";
const symptomOf = (g: Graph) => Object.values(g.nodes).find((n) => n.isSymptom);
function chainTop(g: Graph): GraphNode | undefined {
  const p = bestPathFromSymptom(g);
  return p ? [...p.nodeIds].reverse().map((id) => g.nodes[id]!).find((n) => n.kind === "action") : symptomOf(g);
}

// Untested stories and unread rare records are easy to leave behind once a thread is producing (a story's
// predictions once stayed open for 30 turns while the run followed another thread back).
const STALE_STORIES = 6; // turns without a test or a new round before the stories are brought up
const RARE_AFTER = 6; // turns before rare records nothing has looked at are brought up
const openTests = (g: Graph) => (g.theories ?? []).filter((t) => t.status === "live").flatMap((t) => t.predictions.map((p) => ({ t, p }))).filter(({ p }) => p.status === "open");
function storiesTouched(g: Graph): number {
  return Math.max(g.theorizedTurn ?? 0, ...(g.theories ?? []).flatMap((t) => t.predictions.map((p) => p.testedTurn ?? 0)));
}
// Rare-activity records (rare.ts) that no event, artifact or tested prediction mentions yet.
export function unreadRare(g: Graph): RareValue[] {
  const seen = [
    ...Object.values(g.nodes).map((n) => `${n.label} ${n.artifacts.map((a) => a.value).join(" ")}`),
    ...(g.theories ?? []).flatMap((t) => t.predictions).filter((p) => p.status !== "open").map((p) => `${p.spl} ${p.expect} ${p.result ?? ""}`),
  ].join("\n");
  return rareValues(g.rare).filter((r) => !mentionsRare(seen, [r.value]));
}

// Every move the model may pick, by move: the targets it is available on now.
export function options(g: Graph, cfg: AgentConfig): Partial<Record<Move, string[]>> {
  const nodes = Object.values(g.nodes);
  const o: Partial<Record<Move, string[]>> = {
    expand: nodes.filter((n) => live(n) && !n.isRoot && n.expandAttempts < cfg.maxExpandAttempts + 1).map((n) => n.id),
    strengthen: [...nodes.filter((n) => live(n) && !isStalled(n, cfg)), ...Object.values(g.edges).filter((e) => !e.retired && e.status !== "ruled-out" && g.nodes[e.source]?.kind !== "boundary-marker" && !isStalled(e, cfg))].map((x) => x.id),
    fork: nodes.filter((n) => live(n) && n.forkCount < cfg.maxForksPerNode && getParents(g, n.id).some((e) => !e.retired)).map((n) => n.id),
    branch: nodes.filter((n) => live(n) && !n.isSymptom && !n.deadEnd && (n.branchAttempts ?? 0) < 2).map((n) => n.id),
    pivot: nodes.filter((n) => n.kind === "boundary-marker" && !n.resolvedTo && !pivotExhausted(n, cfg)).map((n) => n.id),
    test: (g.theories ?? []).filter((t) => t.status === "live").flatMap((t) => t.predictions).filter((p) => p.status === "open").map((p) => p.id),
    theorize: (g.theorizeCount ?? 0) < cfg.maxTheorize && symptomOf(g) ? [symptomOf(g)!.id] : [],
    connect: toConnect(g).length && chainTop(g) ? [chainTop(g)!.id] : [],
  };
  for (const k of Object.keys(o) as Move[]) if (!o[k]!.length) delete o[k];
  return o;
}

export function optionsText(g: Graph, cfg: AgentConfig, st: LeadState, turn = 0): string {
  const o = options(g, cfg);
  const open = openTests(g);
  const since = turn - storiesTouched(g);
  const stories = open.length && (g.theorizeCount ?? 0) > 0 && since >= STALE_STORIES
    ? `\nYour stories have untested predictions (${since} turns since a test or a new round): ${open.slice(0, 6).map(({ t, p }) => `${p.id} (${t.id}, ${p.sourcetype}): ${p.expect.slice(0, 90)}`).join("; ")}. Test the one that would tell the stories apart, or revise the stories, before going deeper on one thread.`
    : "";
  const rare = unreadRare(g);
  const rareLine = rare.length && turn >= RARE_AFTER
    ? `\nRare records nothing in the graph has looked at yet: ${rare.slice(0, 4).map((r) => `"${r.value}" (${r.field}, ${r.sourcetype}, ×${r.count})`).join("; ")}. probe <value> reads one over the whole range and what followed it.`
    : "";
  const avail = (Object.entries(o) as [Move, string[]][]).map(([m, ids]) =>
    m === "theorize" ? "theorize" : m === "connect" ? `connect (unlinked: ${toConnect(g).map((n) => n.id).join(", ")})` : `${m} ${ids.slice(0, 25).join(", ")}${ids.length > 25 ? ` …+${ids.length - 25}` : ""}`);
  return `<next_move>
You lead this investigation. In your submission, name the next move in \`next\` (move, target, optional source, why): the strongest lead your searches opened, not a checklist. Follow a thread while it is producing; switch when it goes cold.
Moves: expand <event> = find what caused it; strengthen <event|link> = check it happened / holds; fork <event> = a competing cause; branch <event> = what else it led to; test <prediction> = run a story's prediction; theorize = write or revise competing stories of the incident; pivot <boundary> = follow into a source that holds the cause (set source to the sourcetype to search; any sourcetype); connect = link unlinked findings to the chain; probe <rare value> = read a rare record over the whole range and what came after it. source on any move says where to look first.
Target a graph id listed below, or an event you report in this submission by its exact label. Leave \`next\` out and the system picks.${st.refused ? `\nYour last choice was not used: ${st.refused}` : ""}
available now: ${avail.join("; ") || "none"}${rare.length ? `; probe (${rare.length} rare records unread)` : ""}${stories}${rareLine}
</next_move>`;
}

// The model's choice, checked against the graph after this turn's patch. Labels resolve to events created this
// turn (the model cannot know their ids yet).
export function resolveNext(next: NextIn | undefined, g: Graph, cfg: AgentConfig, turn: number, created: string[], env?: { index: string; range: [string, string] }): { decision?: FrontierDecision; refused?: string } {
  if (!next?.move) return {};
  const source = next.source?.trim() || undefined;
  // probe: a rare record becomes a prediction (whole range, forward from it) and the next turn tests it.
  if (next.move === "probe") {
    const want = (next.target ?? "").trim();
    const r = unreadRare(g).find((x) => x.value === want) ?? unreadRare(g).find((x) => want && (x.value.includes(want) || want.includes(x.value)));
    if (!r || !env) return { refused: `probe ${want} is not available (unread rare records: ${unreadRare(g).slice(0, 6).map((x) => x.value).join(", ") || "none"}).` };
    const q = (v: string) => `"${v.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
    const n = (g.theories ?? []).flatMap((t) => t.predictions).filter((p) => p.id.startsWith("PROBE")).length + 1;
    const p: Prediction = { id: `PROBE${n}`, sourcetype: r.sourcetype, expect: `the ${r.count} record(s) where ${r.field} is ${r.value}: what they are, which host and account, what followed them in time in any source, and whether they lead to anything in the graph`, spl: `index=${env.index} sourcetype=${q(r.sourcetype)} ${q(r.value)} | table _time *`, earliest: env.range[0], latest: env.range[1], separates: [], next: false, status: "open" };
    let t = (g.theories ?? []).find((x) => x.id === "PROBES" && x.status === "live");
    if (!t) { t = { id: "PROBES", round: g.theorizeCount ?? 0, story: "Rare records read on their own (not a story).", accountsFor: [], gaps: "", predictions: [], status: "live", support: 0, against: 0 }; (g.theories ??= []).push(t); }
    t.predictions.push(p);
    return { decision: { move: "test", targetId: p.id, turn: turn + 1, priority: -1, reason: `model probe: ${(next.why ?? "").slice(0, 200)}`, ...(source ? { source } : {}) } };
  }
  const move = next.move as Move;
  const o = options(g, cfg);
  const ids = o[move];
  if (!ids) return { refused: `${next.move} is not available now.` };
  let target = next.target?.trim() ?? "";
  if (move === "theorize" || move === "connect") target = ids[0]!;
  // An event reported this turn, named by its label (the model cannot know its id yet).
  if (!ids.includes(target) && !g.nodes[target] && !g.edges[target]) {
    const want = target.toLowerCase();
    const hit = created.map((id) => g.nodes[id]).find((n) => n && want && (n.label.toLowerCase() === want || n.label.toLowerCase().startsWith(want.slice(0, 60))));
    if (hit) target = hit.id;
  }
  // A pivot named by the event its boundary hangs off: that event's open boundary.
  if (move === "pivot" && g.nodes[target]?.kind === "action") target = ids.find((m) => g.nodes[m]!.originNodeId === target) ?? target;
  // A prediction named as theorize wrote it ("P3", "H1.P3"): the latest round's prediction with that id.
  if (move === "test" && !ids.includes(target)) {
    const key = (x: string) => x.toUpperCase().replace(/[^A-Z0-9]+/g, ".");
    const want = key(target);
    const [h, pp] = want.split(".").length === 2 ? want.split(".") : ["", ""];
    const inStory = (id: string) => !!h && (g.theories ?? []).some((t) => key(t.id).endsWith(`.${h}`) && t.predictions.some((x) => x.id === id) && key(id).endsWith(`.${pp}`));
    const hits = ids.filter((id) => key(id).endsWith(`.${want}`) || inStory(id));
    const round = Math.max(0, ...hits.map((id) => Number(id.match(/^R(\d+)\./)?.[1] ?? 0)));
    const latest = hits.filter((id) => id.startsWith(`R${round}.`));
    if (latest.length === 1) target = latest[0]!;
  }
  if (!ids.includes(target)) return { refused: `${move} ${next.target ?? ""} is not available (${move} is available on: ${ids.slice(0, 12).join(", ")}).` };
  return { decision: { move, targetId: target, turn: turn + 1, priority: -1, reason: `model: ${(next.why ?? "").slice(0, 200)}`, ...(source ? { source } : {}) } };
}
