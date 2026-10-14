import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseYaml } from "yaml";
import { isStructured, RECORD_MODES, type ConclusionSpec, type GuardSpec, type PluginManifest, type RecordMode, type SkillSpec } from "../contracts/plugin.js";
import { SpecError } from "../core/spec.js";

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

export function parsePlugin(text: string, available: readonly SkillSpec[], where = "plugin.yaml"): PluginManifest {
  const raw = parseYaml(text) as Record<string, unknown> | null;
  if (typeof raw !== "object" || raw === null) throw new SpecError(`${where} is not a mapping`);

  const id = text_(raw, "id", where);
  const declared = strings(raw["skills"] ?? [], `${where}: skills`);
  const skills = declared.map((name) => {
    const found = available.find((skill) => skill.name === name);
    if (found === undefined) throw new SpecError(`${where} lists skill ${name}, which has no SKILL.md`);
    return found;
  });

  const conclusion = conclusionOf(raw["conclusion"], where);
  const guard = raw["guard"] === undefined ? undefined : guardOf(raw["guard"], conclusion, where);
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

  return {
    id,
    description: text_(raw, "description", where),
    system: text_(raw, "system", where),
    needs: strings(raw["needs"] ?? [], `${where}: needs`),
    skills,
    conclusion,
    ...(guard === undefined ? {} : { guard }),
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
  // A field the schema never asks for is one the run can never fill.
  for (const field of [summary_field, citations_field, open_questions_field]) {
    if (field !== undefined && !(field in properties)) throw new SpecError(`${where}: conclusion names ${field}, which the schema does not declare`);
  }
  return {
    schema: schema as Record<string, unknown>,
    summary_field,
    ...(citations_field === undefined ? {} : { citations_field }),
    ...(open_questions_field === undefined ? {} : { open_questions_field }),
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
  if (guarded.includes(fallback)) throw new SpecError(`${where}: guard.fallback ${fallback} is itself guarded`);
  return { field, guarded, fallback, reviewer_prompt: text_(raw, "reviewer_prompt", `${where}: guard`) };
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
