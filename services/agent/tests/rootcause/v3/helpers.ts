import { defaultConfig, type AgentConfig } from "../../../plugins/rootcause/v3/config.js";
import { applyPatch, createGraph, emptyPatch, type EvidenceDraft, type Graph, type GraphPatch, type NodeIn } from "../../../plugins/rootcause/v3/graph.js";

export const cfg: AgentConfig = { ...defaultConfig, maxTheorize: 0, linkNeedsSharedValue: false }; // stories are tested on their own (stories.test.ts); shared values in connect.test.ts

export const ev = (strength: EvidenceDraft["strength"], polarity: EvidenceDraft["polarity"] = "supports", toolCallIds = ["tc_1"], summary = `${strength} ${polarity}`): EvidenceDraft =>
  ({ source: "edr", summary, strength, polarity, toolCallIds, traceIds: [], turn: 1 });

export const node = (tempId: string, over: Partial<NodeIn> = {}): NodeIn =>
  ({ tempId, kind: "action", label: tempId, domain: "edr", prior: 0.3, evidence: [], artifacts: [], ...over });

export function apply(g: Graph, p: Partial<GraphPatch>, turn = 1) {
  return applyPatch(g, { ...emptyPatch(), ...p }, turn, cfg);
}

// symptom S (confirmed) <- edges built by the caller
export function seeded(): Graph {
  const g = createGraph("t");
  apply(g, { addNodes: [node("S", { isSymptom: true, label: "symptom", evidence: [ev("direct", "supports", ["tc_s"])] })] });
  return g;
}
