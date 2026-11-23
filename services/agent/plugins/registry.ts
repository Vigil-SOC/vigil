import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseYaml } from "yaml";
import { isStructured, RECORD_MODES, type ConclusionSpec, type GuardSpec, type PluginManifest, type RecordMode, type SkillSpec } from "../contracts/plugin.js";
import { SpecError } from "../core/spec.js";
import { CONFIDENCES, DISPOSITIONS } from "../contracts/events.js";

// A plugin is a directory holding plugin.yaml; its skills come from the repo's one library,
// core/skills/library, resolved against the package: dist/skills in the image, else three levels up.
const ROOT = fileURLToPath(new URL(".", import.meta.url));
const SKILLS = [new URL("../skills/", import.meta.url), new URL("../../../core/skills/library/", import.meta.url)]
  .map((url) => fileURLToPath(url))
  .find((dir) => existsSync(dir)) ?? fileURLToPath(new URL("../skills/", import.meta.url));

const cache = new Map<string, PluginManifest>();

export function pluginFor(id: string, root: string = ROOT, skills: string = SKILLS): PluginManifest {
  const key = `${root}\n${skills}\n${id}`;
  const held = cache.get(key);
  if (held !== undefined) return held;
  const dir = join(root, id);
  const file = join(dir, "plugin.yaml");
  if (!existsSync(file)) throw new SpecError(`no plugin is named ${id}; looked for ${file}`);
  const manifest = parsePlugin(readFileSync(file, "utf8"), loadSkills(skills), file);
  if (manifest.id !== id) throw new SpecError(`${file} declares id ${manifest.id}, but lives under ${id}`);
  cache.set(key, manifest);
  return manifest;
}

// An operator's manifest, carried on a job or a check request rather than read from
// the tree. Carried skills first: the first match by name wins, and the run should use
// the body it journaled, not a bundled one an upgrade may since have changed. Never cached.
export const OPERATOR_MANIFEST = "<operator manifest>";

export function operatorPlugin(text: unknown, carried: unknown): PluginManifest {
  if (typeof text !== "string" || text.trim() === "") throw new SpecError(`${OPERATOR_MANIFEST} must be non-empty YAML text`);
  return parsePlugin(text, [...carriedSkills(carried), ...loadSkills(SKILLS)], OPERATOR_MANIFEST, { operator: true });
}

export function carriedSkills(value: unknown, of: string = OPERATOR_MANIFEST): SkillSpec[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new SpecError(`${of}: skills must be a list`);
  return value.map((one: unknown, i) => {
    if (typeof one !== "object" || one === null) throw new SpecError(`${of}: skill ${i} must be a mapping`);
    const raw = one as Record<string, unknown>;
    const where = `${of}: skill ${i}`;
    const name = text_(raw, "name", where);
    const description = text_(raw, "description", where);
    // Either shape: the text itself (a bundled skill, or a run journaled before the
    // text was left out), or its digest, the text being read when it is loaded.
    if (raw["body"] === undefined) return { name, description, digest: text_(raw, "digest", where) };
    return { name, description, body: text_(raw, "body", where) };
  });
}

// The skills an operator added, carried on a start job: resolved Python-side, with
// the ones a content scan holds already left out. Every run is offered them beside
// its plugin's own, which win a name clash.
export const ADDED_SKILLS = "added_skills";

export function withAddedSkills(plugin: PluginManifest, carried: unknown): PluginManifest {
  const own = new Set(plugin.skills.map((skill) => skill.name));
  const added = carriedSkills(carried, ADDED_SKILLS).filter((skill) => !own.has(skill.name) && own.add(skill.name));
  return added.length === 0 ? plugin : { ...plugin, skills: [...plugin.skills, ...added] };
}

export function parsePlugin(text: string, available: readonly SkillSpec[], where = "plugin.yaml", options: { operator?: boolean } = {}): PluginManifest {
  let raw: Record<string, unknown> | null;
  try {
    raw = parseYaml(text) as Record<string, unknown> | null;
  } catch (error) {
    throw new SpecError(`${where} is not valid YAML: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) throw new SpecError(`${where} is not a mapping`);

  const id = text_(raw, "id", where);
  // An operator's id may never shadow a built-in one, nor be taken for one.
  if (options.operator === true && !/^custom-[a-z0-9][a-z0-9_-]*$/.test(id)) {
    throw new SpecError(`${where}: id ${id} must start with custom- followed by lowercase letters, digits, - or _`);
  }
  const declared = strings(raw["skills"] ?? [], `${where}: skills`);
  const skills = declared.map((name) => {
    const found = available.find((skill) => skill.name === name);
    if (found === undefined) throw new SpecError(`${where} lists skill ${name}, which ${options.operator === true ? "is neither carried with it nor bundled" : "has no SKILL.md"}`);
    return found;
  });

  const conclusion = conclusionOf(raw["conclusion"], where);
  const guard = raw["guard"] === undefined ? undefined : guardOf(raw["guard"], conclusion, where);
  const escalation_guard = raw["escalation_guard"] === undefined ? undefined : guardOf(raw["escalation_guard"], conclusion, `${where}: escalation_guard`);
  // One answer, one reviewer: a value both guards claim would be read by whichever came first.
  if (guard !== undefined && escalation_guard !== undefined && guard.field === escalation_guard.field) {
    const both = escalation_guard.guarded.filter((one) => guard.guarded.includes(one));
    if (both.length > 0) throw new SpecError(`${where}: ${both.join(", ")} is guarded by both guard and escalation_guard`);
  }
  // A dismissal closes the case with no person looking where autonomy lets the run
  // act, so a verdict that can dismiss must pass the reviewer's second look first.
  if (isStructured(conclusion) && conclusion.disposition_field !== undefined) {
    const field = conclusion.disposition_field;
    const allowed = ((conclusion.schema["properties"] as Record<string, { enum?: unknown }>)[field]?.enum ?? []) as unknown[];
    // Both, when the enum holds both; a guard may not name a value outside the enum.
    const dismissals = ["benign", "false_positive"].filter((one) => allowed.includes(one));
    if (dismissals.length > 0 && (guard?.field !== field || !dismissals.every((one) => guard.guarded.includes(one)))) {
      throw new SpecError(`${where}: ${field} can say ${dismissals.join(" or ")}, which can close a case, so it needs a guard on ${field} whose guarded holds ${dismissals.join(" and ")}`);
    }
    // An escalation guard reviews every alarm the enum can say, or an unreviewed one slips past it.
    if (escalation_guard?.field === field) {
      const alarms = ["malicious", "suspicious"].filter((one) => allowed.includes(one) && !escalation_guard.guarded.includes(one));
      if (alarms.length > 0) {
        throw new SpecError(`${where}: ${field} can say ${alarms.join(" and ")}, an alarm, so escalation_guard on ${field} must guard ${alarms.join(" and ")} too`);
      }
    }
  }
  const max_turns = raw["max_turns"];
  if (max_turns !== undefined && (typeof max_turns !== "number" || !Number.isInteger(max_turns) || max_turns < 1)) {
    throw new SpecError(`${where}: max_turns must be a positive integer`);
  }
  const record = recordOf(raw["record"], conclusion, where);
  const finish_with = raw["finish_with"];
  if (finish_with !== undefined && finish_with !== "ask" && finish_with !== "tool") throw new SpecError(`${where}: finish_with must be ask or tool`);
  if (finish_with === "tool" && !isStructured(conclusion)) throw new SpecError(`${where}: finish_with: tool needs a structured conclusion to take`);
  const min_results = raw["min_results"];
  if (min_results !== undefined && (typeof min_results !== "number" || !Number.isInteger(min_results) || min_results < 1)) {
    throw new SpecError(`${where}: min_results must be a positive integer`);
  }
  const q = raw["quarantine"] as { reader?: unknown; guidance?: unknown } | undefined;
  if (q !== undefined && (typeof q?.reader !== "string" || typeof q?.guidance !== "string")) throw new SpecError(`${where}: quarantine needs reader and guidance`);
  const quarantine = q === undefined ? undefined : { reader: q.reader as string, guidance: q.guidance as string };

  const needs = strings(raw["needs"] ?? [], `${where}: needs`);
  const optional = strings(raw["optional"] ?? [], `${where}: optional`);
  const stray = optional.filter((need) => !needs.includes(need));
  if (stray.length > 0) throw new SpecError(`${where}: optional names ${stray.join(", ")}, which needs does not`);

  return {
    id,
    description: text_(raw, "description", where),
    system: text_(raw, "system", where),
    needs,
    ...(optional.length === 0 ? {} : { optional }),
    skills,
    conclusion,
    ...(guard === undefined ? {} : { guard }),
    ...(escalation_guard === undefined ? {} : { escalation_guard }),
    ...(max_turns === undefined ? {} : { max_turns: max_turns as number }),
    ...(record === undefined ? {} : { record }),
    ...(min_results === undefined ? {} : { min_results: min_results as number }),
    ...(finish_with === undefined ? {} : { finish_with: finish_with as "ask" | "tool" }),
    ...(quarantine === undefined ? {} : { quarantine }),
  };
}

// A required record has to surface in the answer: its conclusion names the field
// the run declares its gaps in, and the schema requires that field.
function recordOf(value: unknown, conclusion: ConclusionSpec, where: string): RecordMode | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !(RECORD_MODES as readonly string[]).includes(value)) {
    throw new SpecError(`${where}: record must be one of ${RECORD_MODES.join(", ")}`);
  }
  if (value === "required") {
    if (!isStructured(conclusion) || conclusion.open_questions_field === undefined) {
      throw new SpecError(`${where}: a required record needs a structured conclusion that names its open_questions_field`);
    }
    const required = (conclusion.schema["required"] ?? []) as unknown[];
    if (!required.includes(conclusion.open_questions_field)) {
      throw new SpecError(`${where}: the conclusion schema must require ${conclusion.open_questions_field}, where the run declares its gaps`);
    }
  }
  return value as RecordMode;
}

// `conclusion: prose` ends the run on what the agent last said; a mapping with a
// schema ends it on a structured answer.
function conclusionOf(value: unknown, where: string): ConclusionSpec {
  if (value === "prose") return { schema: null };
  if (typeof value !== "object" || value === null) throw new SpecError(`${where}: conclusion is required: prose, or a mapping with a schema`);
  const raw = value as Record<string, unknown>;
  const schema = raw["schema"];
  if (typeof schema !== "object" || schema === null) throw new SpecError(`${where}: conclusion.schema is required`);
  const properties = ((schema as Record<string, unknown>)["properties"] ?? {}) as Record<string, unknown>;
  const summary_field = text_(raw, "summary_field", `${where}: conclusion`);
  const citations_field = raw["citations_field"] === undefined ? undefined : text_(raw, "citations_field", `${where}: conclusion`);
  const open_questions_field = raw["open_questions_field"] === undefined ? undefined : text_(raw, "open_questions_field", `${where}: conclusion`);
  const confidence_field = raw["confidence_field"] === undefined ? undefined : text_(raw, "confidence_field", `${where}: conclusion`);
  const disposition_field = raw["disposition_field"] === undefined ? undefined : text_(raw, "disposition_field", `${where}: conclusion`);
  const title_field = raw["title_field"] === undefined ? undefined : text_(raw, "title_field", `${where}: conclusion`);
  const proposals_field = raw["proposals_field"] === undefined ? undefined : text_(raw, "proposals_field", `${where}: conclusion`);
  // A field the schema never asks for is one the run can never fill.
  for (const field of [summary_field, citations_field, open_questions_field, confidence_field, disposition_field, title_field, proposals_field]) {
    if (field !== undefined && !(field in properties)) throw new SpecError(`${where}: conclusion names ${field}, which the schema does not declare`);
  }
  if (confidence_field !== undefined) {
    const allowed = (properties[confidence_field] as { enum?: unknown } | undefined)?.enum;
    if (!Array.isArray(allowed) || !allowed.every((one) => (CONFIDENCES as readonly unknown[]).includes(one))) {
      throw new SpecError(`${where}: ${confidence_field} must be an enum drawn from ${CONFIDENCES.join(", ")}`);
    }
  }
  // Exact words only, so the dismissal guard below sees every value that can close a case.
  if (disposition_field !== undefined) {
    const allowed = (properties[disposition_field] as { enum?: unknown } | undefined)?.enum;
    if (!Array.isArray(allowed) || !allowed.every((one) => (DISPOSITIONS as readonly unknown[]).includes(one))) {
      throw new SpecError(`${where}: ${disposition_field} must be an enum drawn from ${DISPOSITIONS.join(", ")}`);
    }
  }
  return {
    schema: schema as Record<string, unknown>,
    summary_field,
    ...(citations_field === undefined ? {} : { citations_field }),
    ...(open_questions_field === undefined ? {} : { open_questions_field }),
    ...(confidence_field === undefined ? {} : { confidence_field }),
    ...(disposition_field === undefined ? {} : { disposition_field }),
    ...(title_field === undefined ? {} : { title_field }),
    ...(proposals_field === undefined ? {} : { proposals_field }),
  };
}

function guardOf(value: unknown, conclusion: ConclusionSpec, where: string): GuardSpec {
  if (typeof value !== "object" || value === null) throw new SpecError(`${where}: guard must be a mapping`);
  const raw = value as Record<string, unknown>;
  const field = text_(raw, "field", `${where}: guard`);
  if (!isStructured(conclusion)) throw new SpecError(`${where}: a guard needs a structured conclusion to read its field from`);
  const properties = ((conclusion.schema["properties"] ?? {}) as Record<string, { enum?: unknown }>);
  const declared = properties[field];
  if (declared === undefined) throw new SpecError(`${where}: guard names ${field}, which the conclusion schema does not declare`);
  const guarded = strings(raw["guarded"], `${where}: guard.guarded`);
  const fallback = text_(raw, "fallback", `${where}: guard`);
  // Against the field's own vocabulary when it has one, so a typo is a load error
  // rather than a guard that never fires.
  if (Array.isArray(declared.enum)) {
    const allowed = new Set(declared.enum);
    for (const one of [...guarded, fallback]) {
      if (!allowed.has(one)) throw new SpecError(`${where}: guard value ${one} is not in ${field}'s enum`);
    }
  }
  // A held dismissal must land somewhere that keeps the case open.
  if (field === conclusion.disposition_field && (fallback === "benign" || fallback === "false_positive")) {
    throw new SpecError(`${where}: guard.fallback on ${field} may not be ${fallback}, which can close the case`);
  }
  // A fallback is written with no review of its own, so it may not claim an attack either.
  if (field === conclusion.disposition_field && (fallback === "malicious" || fallback === "suspicious")) {
    throw new SpecError(`${where}: guard.fallback on ${field} may not be ${fallback}, an alarm no review would stand behind; fall back to a hold such as needs_review`);
  }
  if (guarded.includes(fallback)) throw new SpecError(`${where}: guard.fallback ${fallback} is itself guarded`);
  const min_hypotheses = raw["min_hypotheses"];
  if (min_hypotheses !== undefined && (typeof min_hypotheses !== "number" || !Number.isInteger(min_hypotheses) || min_hypotheses < 1)) {
    throw new SpecError(`${where}: guard.min_hypotheses must be a positive integer`);
  }
  const min_sources = raw["min_sources"];
  if (min_sources !== undefined && (typeof min_sources !== "number" || !Number.isInteger(min_sources) || min_sources < 1)) {
    throw new SpecError(`${where}: guard.min_sources must be a positive integer`);
  }
  const uncounted = raw["uncounted"] === undefined ? undefined : strings(raw["uncounted"], `${where}: guard.uncounted`);
  return {
    field,
    guarded,
    fallback,
    reviewer_prompt: text_(raw, "reviewer_prompt", `${where}: guard`),
    ...(min_hypotheses === undefined ? {} : { min_hypotheses: min_hypotheses as number }),
    ...(min_sources === undefined ? {} : { min_sources: min_sources as number }),
    ...(uncounted === undefined ? {} : { uncounted }),
  };
}

// SKILL.md: a front matter block naming the skill and saying what it is for, then
// the playbook. The name in the file is the name the agent loads it by.
export function loadSkills(dir: string): SkillSpec[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => join(dir, entry.name, "SKILL.md"))
    .filter((file) => existsSync(file))
    .map((file) => parseSkill(readFileSync(file, "utf8"), file))
    .sort((a, b) => (a.name < b.name ? -1 : 1));
}

export function parseSkill(text: string, where = "SKILL.md"): SkillSpec {
  const match = /^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/.exec(text);
  if (match === null) throw new SpecError(`${where} has no front matter`);
  const front = parseYaml(match[1] as string) as Record<string, unknown> | null;
  if (typeof front !== "object" || front === null) throw new SpecError(`${where}: front matter is not a mapping`);
  const body = (match[2] as string).trim();
  if (body === "") throw new SpecError(`${where} has no body`);
  return { name: text_(front, "name", where), description: text_(front, "description", where), body };
}

function text_(raw: Record<string, unknown>, key: string, where: string): string {
  const value = raw[key];
  if (typeof value !== "string" || value.trim() === "") throw new SpecError(`${where}: ${key} must be a non-empty string`);
  return value;
}

function strings(value: unknown, where: string): string[] {
  if (!Array.isArray(value) || value.some((one) => typeof one !== "string" || one === "")) {
    throw new SpecError(`${where} must be a list of non-empty strings`);
  }
  return value as string[];
}
