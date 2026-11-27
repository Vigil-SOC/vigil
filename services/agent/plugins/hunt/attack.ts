import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { scrub } from "../../core/security.js";
import type { EvidenceRecord } from "./types.js";

// The shipped ATT&CK and ATLAS catalogue (core/threat_intel/attack_techniques.json), resolved as
// registry.ts resolves the skill library: dist/threat_intel in the image, else the repo.
const CATALOGUE = [new URL("../../threat_intel/attack_techniques.json", import.meta.url), new URL("../../../../core/threat_intel/attack_techniques.json", import.meta.url)]
  .map((url) => fileURLToPath(url))
  .find((file) => existsSync(file));

interface Entry {
  status: string;
  replaced_by?: string;
}

interface Catalogue {
  version: string;
  techniques: Record<string, Entry>;
  // MITRE ATLAS under its own key and release; every entry is active.
  atlas: { version: string; techniques: Record<string, Entry> };
}

const CITED_CAP = 64;

let held: Catalogue | undefined;

// Read once per process.
export function catalogue(): Catalogue {
  if (held !== undefined) return held;
  if (CATALOGUE === undefined) throw new Error("attack_techniques.json not found beside the agent or in the repo");
  held = JSON.parse(readFileSync(CATALOGUE, "utf8")) as Catalogue;
  return held;
}

export interface TechniqueRefusal {
  claimed_attack_technique: string;
  technique_refusal: string;
  technique_suggestions: string[];
}

// An active id comes back as the catalogue spells it; anything else as a refusal,
// worded as core/detections/gaps.py _not_active_reason words it. This is the only
// TypeScript copy of that rule. A suggestion is never put in place of the cited id.
export function checkTechnique(cited: string): { technique: string } | TechniqueRefusal {
  const { version, techniques: attack, atlas } = catalogue();
  const id = cited.trim().toUpperCase();
  const techniques = id.startsWith("AML.") ? atlas.techniques : attack;
  const entry = techniques[id];
  if (entry?.status === "active") return { technique: id };
  const refusal = (reason: string, suggestions: string[] = []): TechniqueRefusal => ({
    claimed_attack_technique: scrub(cited.trim(), CITED_CAP),
    technique_refusal: reason,
    technique_suggestions: suggestions,
  });
  if (entry?.status === "revoked") {
    return entry.replaced_by ? refusal(`revoked; replaced by ${entry.replaced_by}`, [entry.replaced_by]) : refusal("revoked");
  }
  if (entry?.status === "deprecated") return refusal("deprecated");
  // An unknown sub-technique of an active parent: the parent and its active sub-techniques.
  // The last dot, so AML.T0051.999's parent is AML.T0051.
  const dot = id.lastIndexOf(".");
  const parent = dot > 0 ? id.slice(0, dot) : null;
  const suggestions =
    parent !== null && techniques[parent]?.status === "active"
      ? [parent, ...Object.keys(techniques).filter((one) => one.startsWith(`${parent}.`) && techniques[one]!.status === "active").sort()]
      : [];
  return refusal(`not an ATT&CK technique (release ${version}) or ATLAS technique (release ${atlas.version})`, suggestions);
}

// The refusal a recorded payload carries, if its cited technique was refused.
export function refusalOf(payload: Record<string, unknown>): TechniqueRefusal | null {
  const { claimed_attack_technique, technique_refusal, technique_suggestions } = payload;
  if (typeof claimed_attack_technique !== "string" || typeof technique_refusal !== "string") return null;
  const suggestions = Array.isArray(technique_suggestions) ? technique_suggestions.filter((one): one is string => typeof one === "string") : [];
  return { claimed_attack_technique, technique_refusal, technique_suggestions: suggestions };
}

// The refusals among recorded evidence, for the record_evidence reply.
export function refusalsOf(records: readonly EvidenceRecord[]): ({ evidence_id: string } & TechniqueRefusal)[] {
  return records.flatMap(({ evidence_id, payload }) => {
    const refusal = refusalOf(payload);
    return refusal === null ? [] : [{ evidence_id, ...refusal }];
  });
}
