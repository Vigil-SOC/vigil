// Identity vs. action (spec §9), pure. Actions never carry "who": each identity signal becomes an actor node
// and a weighted edge to the action. Two relations:
//   ran-as (attributed-to): the account, credential or session the action executed under. A log fact.
//   operated-by:            who actually controlled it, when that differs (malware in a victim's session).
// The model tags each signal's layer: a name the actor chose is human-facing and never counts
// above circumstantial; an identity the system issued after authenticating is machine-facing.
// The same identity fact (relation, layer, kind, value) counts once however often it is reported.
import { linkedPrincipals, type EdgeIn, type EdgeKind, type EvidenceDraft, type Graph, type GraphNode, type GraphPatch, type NodeIn } from "./graph.js";
import type { Domain } from "./types.js";

export interface IdentitySignal {
  layer: "human-facing" | "machine-facing";
  relation?: "ran-as" | "operated-by";
  kind: string;
  value: string;
  resolvesTo?: string;
  toolCallIds: string[];
}

export interface AttributionCtx {
  turn: number;
  domain: Domain;
  traceIds: (callIds: string[]) => string[];
}

const low = (s: string) => s.trim().toLowerCase();

// An actor is known by every name it has been reported under (its value and what it resolves to).
function findActor(g: Readonly<Graph>, names: string[]): string | undefined {
  const want = new Set(names.map(low));
  return Object.values(g.nodes).find((n) => n.kind === "actor" && n.artifacts.some((a) => a.kind === "principal" && want.has(low(a.value))))?.id;
}

export function buildAttributionPatch(actionId: string, signals: IdentitySignal[], g: Readonly<Graph>, ctx: AttributionCtx): Pick<GraphPatch, "addNodes" | "addEdges"> {
  const addNodes: NodeIn[] = [];
  const addEdges: EdgeIn[] = [];
  if (!signals.length) return { addNodes, addEdges };

  const ev = (summary: string, strength: EvidenceDraft["strength"], polarity: EvidenceDraft["polarity"], callIds: string[], key: string): EvidenceDraft => ({
    source: ctx.domain, summary: summary.slice(0, 300), strength, polarity, toolCallIds: callIds, traceIds: ctx.traceIds(callIds), turn: ctx.turn, key,
  });

  // Group by relation and principal; a principal's names are its resolvesTo and every raw value seen for it.
  type Group = { relation: "ran-as" | "operated-by"; principal: string; names: Set<string>; sigs: IdentitySignal[] };
  const groups = new Map<string, Group>();
  for (const s of signals) {
    const relation = s.relation ?? "ran-as";
    const principal = (s.resolvesTo ?? s.value).trim();
    if (!principal) continue;
    const k = `${relation}|${low(principal)}`;
    const grp = groups.get(k) ?? groups.set(k, { relation, principal, names: new Set(), sigs: [] }).get(k)!;
    grp.names.add(principal).add(s.value.trim());
    grp.sigs.push(s);
  }

  // "Operated by" the same principal the action ran as says nothing: drop it.
  for (const [k, grp] of groups) if (grp.relation === "operated-by" && groups.has(`ran-as|${low(grp.principal)}`)) groups.delete(k);

  const actorIds = new Map<string, string>(); // lower-cased principal -> actor id (real or temp), shared across relations
  const edgeFor = new Map<string, EdgeIn>();
  for (const [k, grp] of groups) {
    // Always described with all its names: applyPatch merges it into a known actor that shares one, adding new aliases.
    let actorId = actorIds.get(low(grp.principal));
    if (!actorId) {
      actorId = `actor:${grp.principal}`;
      const known = findActor(g, [...grp.names]);
      addNodes.push({ tempId: actorId, kind: "actor", label: known ? g.nodes[known]!.label : grp.principal, domain: ctx.domain, prior: 0.5, evidence: [], artifacts: [...grp.names].map((v) => ({ kind: "principal" as const, value: v })) });
    }
    actorIds.set(low(grp.principal), actorId);
    const kind: EdgeKind = grp.relation === "operated-by" ? "operated-by" : "attributed-to";
    // A system-issued identity on the record is strong prior knowledge of who it ran as (one record then confirms
    // it); a name the actor chose starts low, so a contradiction rules it out.
    // System-issued identities that all belong to one chain (a role and its session) start high; ones that
    // disagree start low and stay disputed.
    const machine = [...groups.values()].filter((x) => x.relation === "ran-as" && x.sigs.some((y) => y.layer === "machine-facing"));
    const asNode = (x: Group) => ({ label: x.principal, artifacts: [...x.names].map((v) => ({ kind: "principal", value: v })) }) as unknown as GraphNode;
    const oneChain = machine.every((x) => machine.every((y) => x === y || linkedPrincipals(asNode(x), asNode(y)) || machine.some((z) => linkedPrincipals(asNode(x), asNode(z)) && linkedPrincipals(asNode(z), asNode(y)))));
    const prior = kind === "attributed-to" && oneChain && grp.sigs.some((x) => x.layer === "machine-facing") ? 0.5 : 0.3;
    const edge: EdgeIn = { tempId: `${kind}:${grp.principal}->${actionId}`, kind, source: actorId, target: actionId, prior, evidence: [] };
    for (const s of grp.sigs) {
      const key = `${kind}|${s.layer}|${s.kind}|${low(s.value)}`;
      if (kind === "operated-by") edge.evidence.push(s.layer === "machine-facing"
        ? ev(`${s.kind} ${s.value} shows ${grp.principal} controlled the action`, "corroborating", "supports", s.toolCallIds, key)
        : ev(`${s.kind} "${s.value}" names ${grp.principal} as operator (spoofable)`, "circumstantial", "supports", s.toolCallIds, key));
      else edge.evidence.push(s.layer === "machine-facing"
        ? ev(`machine-facing ${s.kind} ${s.value}: the action ran as ${grp.principal}`, "direct", "supports", s.toolCallIds, key)
        : ev(`human-facing ${s.kind} "${s.value}" names ${grp.principal} (spoofable)`, "circumstantial", "supports", s.toolCallIds, key));
    }
    edgeFor.set(k, edge);
    addEdges.push(edge);
  }

  // Divergence (ran-as only): a system-issued identity contradicts a name the actor chose.
  const ranAs = [...groups].filter(([, g]) => g.relation === "ran-as");
  const machine = ranAs.filter(([, g]) => g.sigs.some((s) => s.layer === "machine-facing"));
  const human = ranAs.filter(([k, g]) => !machine.some(([m]) => m === k) && g.sigs.some((s) => s.layer === "human-facing"));
  if (machine.length === 1) {
    const [mk, m] = machine[0]!;
    const mSigs = m.sigs.filter((s) => s.layer === "machine-facing");
    for (const [hk, h] of human) {
      const calls = [...new Set([...mSigs, ...h.sigs].flatMap((s) => s.toolCallIds))];
      const shown = h.sigs.map((s) => `${s.kind} "${s.value}"`).join(", ");
      const key = `divergence|${low(m.principal)}|${low(h.principal)}`;
      edgeFor.get(hk)!.evidence.push(ev(`machine identity ${m.principal} contradicts ${shown}`, "direct", "refutes", calls, key));
      edgeFor.get(mk)!.evidence.push(ev(`Identity divergence: action names ${h.principal} (${shown}) but the system-issued identity is ${m.principal}`, "corroborating", "supports", calls, key));
    }
  }
  // Two machine-facing ran-as principals that disagree are left disputed for the frontier (P2).
  return { addNodes, addEdges };
}

export const isDivergence = (summary: string) => summary.startsWith("Identity divergence:");
