// connect: findings in the graph that no link ties to the alert's chain (tests turn up events with no link
// shown). One move lists them with the chain top; the model searches for what passes from one to the next and
// reports the steps in between and the links. A link into a node with an open boundary resolves that boundary:
// its cause has been found.
import type { z } from "zod4";
import type { EdgeIn, GraphPatch } from "../graph.js";
import { toConnect } from "../frontier.js";
import { instructions } from "../model/prompts.js";
import type { SubmitLinks } from "../model/schemas.js";
import { candidatePatch, merge, noteSuspicious, tempId, userContent, Vetter, type Handler } from "./common.js";

const SKEW_MS = 60_000; // a cause may carry a time up to a minute after its effect (clock skew across sources)

export const connect: Handler = async (d, g, ctx) => {
  const top = d.targetId;
  const ids = toConnect(g).map((n) => n.id);
  const done: Partial<GraphPatch> = { incrementConnect: ids };
  if (!ids.length) return merge(done);
  const r = await ctx.model.runTurn({ move: "connect", correlationId: top, submitTool: "submit_links", allowedDomains: ctx.domains, trace: ctx.trace,
    userContent: userContent(g, top, instructions.connect(g, top, ids), ctx, ctx.domains) });
  const sub = r.submission as z.infer<typeof SubmitLinks> | null;
  if (!sub) return merge(done);
  noteSuspicious(sub, ctx);
  const v = new Vetter(ctx, r.toolCalls);

  // New events: nodes only; the links below carry the edges.
  const parts: Partial<GraphPatch>[] = [];
  const refs = new Map<string, { id: string; at?: string; legitimate?: boolean }>();
  for (const e of sub.events) {
    if (refs.has(e.ref) || g.nodes[e.ref]) { ctx.trace.write("rejected-evidence", { event: e.label, reason: `ref ${e.ref} reused` }); continue; }
    const x = candidatePatch({ ...e, edgeEvidence: [] }, v, g, ctx);
    refs.set(e.ref, { id: x.id, at: x.node.observedAt, legitimate: x.node.legitimate });
    parts.push(x.patch);
  }
  const end = (k: string) => {
    const n = g.nodes[k];
    if (n) return n.kind === "action" && n.status !== "ruled-out" ? { id: n.id, at: n.observedAt, legitimate: n.legitimate, existing: true } : undefined;
    const x = refs.get(k);
    return x && { ...x, existing: false };
  };

  const edges: EdgeIn[] = [];
  let resolve: GraphPatch["resolveBoundary"];
  for (const l of sub.links) {
    const a = end(l.from), b = end(l.to);
    const evidence = v.evidence(l.evidence, a && g.nodes[a.id] ? g.nodes[a.id].domain : ctx.domains[0]);
    const why = !a || !b ? `unknown end ${!a ? l.from : l.to}`
      : a.id === b.id ? "a link joins two different events"
      : a.at && b.at && Date.parse(a.at) > Date.parse(b.at) + SKEW_MS ? `${l.from} (${a.at}) is after ${l.to} (${b.at}); a cause comes first`
      : !evidence.length ? "no cited evidence"
      // Routine work joins the attack's chain only on a record of the join (a developer's routine registration
      // was once linked to a stolen role's first call on timing alone).
      : a.legitimate && !b.legitimate && !evidence.some((x) => x.strength === "direct" && x.polarity === "supports") ? "a legitimate step links into the attack only with direct evidence"
      : "";
    // Loose: a legitimate step's link without direct evidence is kept as inferred.
    const inferred = ctx.cfg.loose && why === "a legitimate step links into the attack only with direct evidence";
    if (inferred) for (const x of evidence) x.strength = "circumstantial";
    if (why && !inferred) { ctx.trace.write("rejected-evidence", { link: l, reason: why }); continue; }
    // The cause of a node with an open boundary has been found: resolve the boundary with this link.
    const marker = b!.existing && !resolve ? Object.values(g.nodes).find((m) => m.kind === "boundary-marker" && m.originNodeId === b!.id && !m.resolvedTo) : undefined;
    if (marker) resolve = { markerId: marker.id, nodeId: a!.id, joinEvidence: evidence };
    else edges.push({ tempId: tempId("link"), kind: "explains", source: a!.id, target: b!.id, prior: l.prior, evidence, ...(inferred ? { inferred: true } : {}) });
  }
  if (sub.unrelated?.length) ctx.trace.write("reasoning", { connect: top, unrelated: sub.unrelated });
  return merge(done, ...parts, { addEdges: edges, ...(resolve ? { resolveBoundary: resolve } : {}) });
};
