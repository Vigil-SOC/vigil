import { topologyFor, type TopologyId } from "./topology.js";
import { readFileSync } from "node:fs";
import { parse as parseYaml } from "yaml";
import { DEFAULT_PARK_MS, type BudgetLimits } from "../contracts/budget.js";

export class SpecError extends Error {}

// Serial is parallel with one worker, so there is no second loop to maintain.
export interface DispatchPolicy {
  topology: TopologyId;
  mode: "serial" | "parallel";
  fan_out_over: string;
  max_workers: number;
}

// Opaque counts: what a window means belongs to whoever folds the ledger, and the
// harness would have to know a domain to check any of them.
export type Counts = Readonly<Record<string, number>>;

// One role: what it is told, what shape it must answer in, what it may call.
// description is the one line the lead reads when choosing a worker.
export interface RoleSpec {
  prompt: string;
  description: string;
  // null for a role declared `output_schema: prose`. A missing key stays the error
  // it was, so a typo cannot quietly turn a role conversational.
  output_schema: Record<string, unknown> | null;
  tools: string[];
  // What the role needs, rather than what this deployment happens to call it. An
  // arch is versioned and shared, so it names a capability and the config binds it.
  needs: string[];
}

export const PROSE = "prose";

// Worker keys are the ids the lead may name. The lead is optional too: an arch
// whose order is authored elsewhere has nothing for one to decide.
export interface Roles {
  lead?: RoleSpec;
  workers: Record<string, RoleSpec>;
  critic?: RoleSpec;
}

// The shape of a loop, operator-authored and never uploaded.
export interface ArchSpec {
  name: string;
  roles: Roles;
  dispatch: DispatchPolicy;
  digest: Counts;
}

// One authored step: which agent runs it, what it is told, and whether it stops
// for a human first. Order is the list's, so an agent may appear more than once.
export interface PhaseSpec {
  id: string;
  agent: string;
  name: string;
  instructions: string;
  approval_required: boolean;
  // Authored: what this step may call, narrowed from what its agent holds.
  tools: string[];
  // Resolved, not authored: an agent's own prompt is rendered at run start, so a
  // file leaves this empty and whoever resolves the reference fills it.
  prompt: string;
}

// The uploadable layer: the scenario, and what an analyst should know. No schemas.
// It is also the registry for it — the catalog reads these fields, never restates them.
export interface Playbook {
  sections: Record<string, unknown>;
  name: string;
  description: string;
  use_case: string;
  trigger_examples: string[];
  objectives: string[];
  scope: Record<string, unknown>;
  directives: Record<string, string>;
  // Ordered, unlike directives: a Record keyed by role cannot say one role runs,
  // then a second, then the first again with something else to do.
  phases: PhaseSpec[];
  narrative: string;
}

export interface ToolSpec {
  id: string;
  kind: string;
  [key: string]: unknown;
}

export interface Runtime {
  max_turns: number;
  result_cap: number;
  recall_limit: number;
}

// Deployment: where this points, what it may spend, what it may call, which calls
// stop for a human, and the numbers a workflow measures itself against.
export interface Config {
  sections: Record<string, unknown>;
  model: string;
  budgets: BudgetLimits;
  runtime: Runtime;
  tools: ToolSpec[];
  approvals: string[];
  thresholds: Counts;
}

export interface RunSpec extends Config, Omit<Playbook, "directives"> {
  // Whatever the workflow declared it owns, untouched. The harness never reads it.
  sections: Record<string, unknown>;
  arch: string;
  roles: Roles;
  dispatch: DispatchPolicy;
  digest: Counts;
  // The job's, not a file's: the playbook says what this kind of run does and
  // this says which one. Journalled with the spec, so a resume still knows.
  prompt: string;
}

// Reserved directive key: prose every worker needs, such as what the data is.
export const ALL_WORKERS = "workers";

export const DEFAULT_DISPATCH: DispatchPolicy = { topology: "fan_out", mode: "serial", fan_out_over: "questions", max_workers: 1 };

// The only coherent default: an arch with no workers dispatches to nobody, and
// one with workers fans out to them. Declaring a topology still overrides it.
function defaultDispatch(workers: readonly string[]): DispatchPolicy {
  return { ...DEFAULT_DISPATCH, topology: workers.length === 0 ? "single" : "fan_out" };
}
export const DEFAULT_BUDGETS: BudgetLimits = {
  max_calls: 12,
  max_cost_usd: 5,
  max_wall_ms: 1_800_000,
  max_park_ms: DEFAULT_PARK_MS,
};
export const DEFAULT_RUNTIME: Runtime = { max_turns: 8, result_cap: 20_000, recall_limit: 3 };

// Disjoint by design: a key in the wrong file is a load error rather than a silent
// default, and there is no precedence chain to reason about.
const LAYERS = {
  arch: ["name", "roles", "dispatch", "digest"],
  playbook: [
    "name",
    "description",
    "use_case",
    "trigger_examples",
    "objectives",
    "scope",
    "directives",
    "phases",
    "narrative",
  ],
  config: ["model", "budgets", "runtime", "tools", "approvals", "thresholds"],
} as const;

export type Layer = keyof typeof LAYERS;

// Sections a workflow owns, declared by its registry entry. The loader accepts
// them and validates nothing: what they mean is the workflow's, not the harness's.
export type Owned = Partial<Record<Layer, readonly string[]>>;

const NONE: Owned = {};

function allowed(layer: Layer, owned: Owned): readonly string[] {
  return [...LAYERS[layer], ...(owned[layer] ?? [])];
}

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (value === null || value === undefined) return {};
  if (typeof value !== "object" || Array.isArray(value)) throw new SpecError(`${what} must be a mapping`);
  return value as Record<string, unknown>;
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function strings(value: unknown, field: string): string[] {
  if (value === undefined || value === null) return [];
  if (typeof value === "string") return [value];
  if (!Array.isArray(value)) throw new SpecError(`${field} must be a string or a list of strings`);
  return value.map(String);
}

// One merge for every block that is a fixed key set over defaults, so an unknown
// key is refused in the same shape wherever it appears.
function merge<T extends object>(raw: unknown, defaults: T, what: string): T {
  const record = asRecord(raw, what);
  const stray = Object.keys(record).filter((key) => !(key in defaults));
  if (stray.length > 0) {
    throw new SpecError(
      `unknown ${what} key(s): ${stray.sort().join(", ")}; expected any of ${Object.keys(defaults).sort().join(", ")}`,
    );
  }
  return { ...defaults, ...record };
}

function counts(raw: unknown, what: string): Counts {
  const record = asRecord(raw, what);
  for (const [field, value] of Object.entries(record)) {
    if (!Number.isInteger(value) || (value as number) < 0) {
      throw new SpecError(`${what}.${field} must be a non-negative integer, got ${String(value)}`);
    }
  }
  return record as Counts;
}

function positive<T extends object>(block: T, what: string): T {
  for (const [field, value] of Object.entries(block)) {
    if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
      throw new SpecError(`${what}.${field} must be a positive number, got ${String(value)}`);
    }
  }
  return block;
}

function splitFrontMatter(text: string): [Record<string, unknown>, string] {
  const trimmed = text.trimStart();
  if (!trimmed.startsWith("---")) return [asRecord(parseYaml(text), "document"), ""];

  const parts = trimmed.split("---");
  if (parts.length < 3) throw new SpecError("unterminated YAML front matter (expected a closing ---)");
  return [asRecord(parseYaml(parts[1] ?? ""), "front matter"), parts.slice(2).join("---").trim()];
}

// Names the file a stray key belongs in: the three layers are disjoint, so a
// misplaced budgets is a typo with an address, not an unknown key.
function placed(key: string, layer: Layer, owned: Owned): string {
  const owner = (Object.keys(LAYERS) as Layer[]).find((other) => allowed(other, owned).includes(key));
  if (owner !== undefined) return `${key} belongs in the ${owner} file, not the ${layer} file`;
  return `${key} belongs in no file; a ${layer} file takes any of ${[...allowed(layer, owned)].sort().join(", ")}`;
}

// One reader for all three layers. A misplaced budgets would otherwise hand an
// autonomous run the default budget without saying so.
function read(text: string, layer: Layer, owned: Owned = NONE): [Record<string, unknown>, string] {
  const [front, body] = splitFrontMatter(text);
  const stray = Object.keys(front).filter((key) => !allowed(layer, owned).includes(key));
  if (stray.length > 0) throw new SpecError(stray.sort().map((key) => placed(key, layer, owned)).join("; "));
  return [front, body];
}

function load<T>(path: string, parse: (text: string) => T, layer: Layer): T {
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") throw new SpecError(`no such ${layer} file: ${path}`);
    throw new SpecError(`unreadable ${layer} file ${path}: ${(error as Error).message}`);
  }
  try {
    return parse(text);
  } catch (error) {
    if (error instanceof SpecError) throw new SpecError(`${path}: ${error.message}`);
    throw new SpecError(`invalid ${layer} file ${path}: ${(error as Error).message}`);
  }
}

// mode is checked against max_workers rather than coerced into it: silently
// rewriting a count the operator wrote makes mode a field nothing reads.
function parseDispatch(raw: unknown, workers: readonly string[]): DispatchPolicy {
  const policy = merge(raw, defaultDispatch(workers), "dispatch");
  // Throws on an unknown name, so an arch naming a shape nothing implements
  // fails at load rather than running as whatever the default happened to be.
  topologyFor(policy.topology);
  if (policy.mode !== "serial" && policy.mode !== "parallel") {
    throw new SpecError(`dispatch.mode must be serial or parallel, got ${String(policy.mode)}`);
  }
  if (str(policy.fan_out_over).trim() === "") throw new SpecError("dispatch.fan_out_over must name what an iteration fans out over");
  if (!Number.isInteger(policy.max_workers) || policy.max_workers < 1) {
    throw new SpecError(`dispatch.max_workers must be a positive integer, got ${String(policy.max_workers)}`);
  }
  if (policy.mode === "serial" && policy.max_workers !== 1) {
    throw new SpecError(`dispatch.mode serial is max_workers 1, so ${policy.max_workers} contradicts it; say parallel or drop the count`);
  }
  if (policy.topology === "single" && workers.length > 0) {
    throw new SpecError(`dispatch.topology single dispatches to nobody, so ${workers.length} worker(s) contradicts it`);
  }
  if (policy.topology !== "single" && workers.length === 0) {
    throw new SpecError(`dispatch.topology ${policy.topology} needs workers, and the arch declares none`);
  }
  return policy;
}

// An arch may drop an action its pipeline has no use for, but one no workflow
// handles is a dead end the lead would keep choosing.
function assertVocabulary(schema: Record<string, unknown>, handled: readonly string[]): void {
  const properties = asRecord(schema["properties"], "roles.lead.output_schema.properties");
  const declared = asRecord(properties["action"], "roles.lead.output_schema.properties.action")["enum"];
  if (!Array.isArray(declared) || declared.length === 0) {
    throw new SpecError("roles.lead.output_schema needs a non-empty action enum");
  }
  const invented = declared.map(String).filter((action) => !handled.includes(action));
  if (invented.length > 0) {
    throw new SpecError(`roles.lead declares action(s) no workflow handles: ${invented.sort().join(", ")}`);
  }
}

function parseRole(raw: unknown, name: string): RoleSpec {
  const record = asRecord(raw, `roles.${name}`);
  const prompt = str(record["prompt"]);
  if (prompt.trim() === "") throw new SpecError(`roles.${name} needs a prompt`);
  const declared = record["output_schema"];
  if (declared === undefined) throw new SpecError(`roles.${name} needs an output_schema`);

  return {
    prompt,
    description: str(record["description"]),
    output_schema: declared === PROSE ? null : asRecord(declared, `roles.${name}.output_schema`),
    tools: strings(record["tools"], `roles.${name}.tools`),
    needs: strings(record["needs"], `roles.${name}.needs`),
  };
}

// preamble is the discipline every specialist shares, so the arch states it once
// instead of repeating it per worker.
function parseWorkers(raw: unknown, preamble: string): Record<string, RoleSpec> {
  return Object.fromEntries(
    Object.entries(asRecord(raw, "roles.workers")).map(([id, value]) => {
      const role = parseRole(value, `workers.${id}`);
      // Without it the generated roster is blank and the lead chooses on the id alone.
      if (role.description.trim() === "") throw new SpecError(`roles.workers.${id} needs a description`);
      return [id, preamble ? { ...role, prompt: `${preamble}\n\n${role.prompt}` } : role];
    }),
  );
}

const ROLE_GROUPS = ["lead", ALL_WORKERS, "workers_preamble", "critic"];

function parseRoles(raw: unknown, handled: readonly string[]): Roles {
  const record = asRecord(raw, "roles");
  const stray = Object.keys(record).filter((key) => !ROLE_GROUPS.includes(key));
  if (stray.length > 0) {
    throw new SpecError(`unknown role group(s): ${stray.sort().join(", ")}; expected any of ${[...ROLE_GROUPS].sort().join(", ")}`);
  }

  const roles: Roles = { workers: parseWorkers(record[ALL_WORKERS], str(record["workers_preamble"]).trim()) };
  // No lead is a declaration that the order is authored elsewhere, so there is no
  // vocabulary to check: a workflow that sequences its own phases decides nothing.
  if (record["lead"] !== undefined) {
    const lead = parseRole(record["lead"], "lead");
    // A conversational lead chooses nothing, so there is no vocabulary to check.
    if (lead.output_schema !== null) assertVocabulary(lead.output_schema, handled);
    roles.lead = lead;
  }
  if (record["critic"] !== undefined) roles.critic = parseRole(record["critic"], "critic");
  return roles;
}

export function parseArch(text: string, handled: readonly string[]): ArchSpec {
  const [front] = read(text, "arch");
  const roles = parseRoles(front["roles"], handled);
  return {
    name: str(front["name"]) || "unnamed",
    roles,
    dispatch: parseDispatch(front["dispatch"], Object.keys(roles.workers)),
    digest: counts(front["digest"], "digest"),
  };
}

// Ids are the playbook's, not generated: a resumed run finds its place by phase
// id, so one that changed between enqueue and resume would restart the wrong step.
function parsePhases(raw: unknown): PhaseSpec[] {
  if (raw === undefined || raw === null) return [];
  if (!Array.isArray(raw)) throw new SpecError("phases must be a list");

  const seen = new Set<string>();
  return raw.map((entry, index) => {
    const phase = asRecord(entry, `phases[${index}]`);
    const id = str(phase["id"]).trim() || `phase-${index + 1}`;
    const agent = str(phase["agent"]).trim();
    if (agent === "") throw new SpecError(`phases[${index}] needs an agent`);
    if (seen.has(id)) throw new SpecError(`phases declares the id ${id} twice; ids address a step, so they cannot repeat`);
    seen.add(id);
    return {
      id,
      agent,
      name: str(phase["name"]).trim() || id,
      instructions: str(phase["instructions"]),
      approval_required: phase["approval_required"] === true,
      prompt: str(phase["prompt"]),
      tools: strings(phase["tools"], `phases[${index}].tools`),
    };
  });
}

// Role names are checked against the arch's registry in applyDirectives, not
// here: a playbook is read without knowing which arch it will run under.
export function parsePlaybook(text: string, owned: Owned = NONE): Playbook {
  const [front, body] = read(text, "playbook", owned);
  const sections = Object.fromEntries((owned["playbook"] ?? []).filter((key) => key in front).map((key) => [key, front[key]]));
  const directives = asRecord(front["directives"], "directives");
  return {
    sections,
    name: str(front["name"]),
    description: str(front["description"]),
    use_case: str(front["use_case"]),
    trigger_examples: strings(front["trigger_examples"], "trigger_examples"),
    objectives: strings(front["objectives"], "objectives"),
    scope: asRecord(front["scope"], "scope"),
    directives: Object.fromEntries(Object.entries(directives).map(([role, value]) => [role, String(value)])),
    phases: parsePhases(front["phases"]),
    narrative: body || str(front["narrative"]),
  };
}

function parseTools(raw: unknown): ToolSpec[] {
  if (raw === undefined || raw === null) return [];
  if (!Array.isArray(raw)) throw new SpecError("tools must be a list");
  return raw.map((entry, index) => {
    const tool = asRecord(entry, `tools[${index}]`);
    if (typeof tool["id"] !== "string" || typeof tool["kind"] !== "string") {
      throw new SpecError(`tools[${index}] needs a string id and a string kind`);
    }
    return tool as ToolSpec;
  });
}

export function parseConfig(text: string, owned: Owned = NONE): Config {
  const [front] = read(text, "config", owned);
  const sections = Object.fromEntries((owned["config"] ?? []).filter((key) => key in front).map((key) => [key, front[key]]));
  const model = str(front["model"]);
  if (model.trim() === "") throw new SpecError("config needs a model: a deployment that names none bills nothing and answers nothing");

  const tools = parseTools(front["tools"]);
  const declared = new Set(tools.map((tool) => tool.id));
  if (declared.size !== tools.length) throw new SpecError("tools declares the same id twice");

  const approvals = strings(front["approvals"], "approvals");
  const ungranted = approvals.filter((id) => !declared.has(id));
  if (ungranted.length > 0) throw new SpecError(`approvals name tool(s) this config does not declare: ${ungranted.sort().join(", ")}`);

  return {
    sections,
    model,
    budgets: positive(merge(front["budgets"], DEFAULT_BUDGETS, "budgets"), "budgets"),
    runtime: positive(merge(front["runtime"], DEFAULT_RUNTIME, "runtime"), "runtime"),
    tools,
    approvals,
    thresholds: counts(front["thresholds"], "thresholds"),
  };
}

function extend(role: RoleSpec, name: string, additions: (string | undefined)[], declared: ReadonlySet<string>): RoleSpec {
  const missing = role.tools.filter((id) => !declared.has(id));
  if (missing.length > 0) {
    throw new SpecError(`arch role ${name} needs tool(s) the config does not declare: ${missing.join(", ")}`);
  }
  const prompt = [role.prompt, ...additions.filter((text) => text)].join("\n\n");
  return prompt === role.prompt ? role : { ...role, prompt };
}

// Playbook prose layers onto the arch prompt rather than replacing it: the
// playbook says what this scenario is, the arch says how to reason about any of them.
function applyDirectives(roles: Roles, directives: Record<string, string>, declared: ReadonlySet<string>): Roles {
  const known = new Set(["lead", ALL_WORKERS, "critic", ...Object.keys(roles.workers)]);
  const stray = Object.keys(directives).filter((key) => !known.has(key));
  if (stray.length > 0) {
    throw new SpecError(`directives name unknown role(s): ${stray.sort().join(", ")}; expected any of ${[...known].sort().join(", ")}`);
  }

  const shared = directives[ALL_WORKERS];
  const workers = Object.entries(roles.workers).map(([id, role]) => [id, extend(role, id, [shared, directives[id]], declared)]);
  const applied: Roles = { workers: Object.fromEntries(workers) as Record<string, RoleSpec> };
  if (roles.lead !== undefined) applied.lead = extend(roles.lead, "lead", [directives["lead"]], declared);
  if (roles.critic !== undefined) applied.critic = extend(roles.critic, "critic", [directives["critic"]], declared);
  return applied;
}

// Generated from the registry rather than written into the prompt, so the roster
// the lead reads cannot drift from the workers that actually exist.
function roster(workers: Record<string, RoleSpec>): string {
  return [
    "## Workers you may dispatch",
    "Name exactly one of these in worker_agent_id when you dispatch.",
    ...Object.entries(workers).map(([id, role]) => `- ${id} — ${role.description}`),
  ].join("\n");
}

// The registry again, as a schema constraint: an unconstrained string is where a
// struggling emission puts its overflow, and the id arrives carrying half a query.
function constrainWorkerId(
  schema: Record<string, unknown> | null,
  workers: Record<string, RoleSpec>,
): Record<string, unknown> | null {
  if (schema === null) return null;
  const properties = asRecord(schema["properties"], "roles.lead.output_schema.properties");
  const field = properties["worker_agent_id"];
  if (typeof field !== "object" || field === null) return schema;
  const ids: (string | null)[] = [...Object.keys(workers), null];
  return { ...schema, properties: { ...properties, worker_agent_id: { ...field, enum: ids } } };
}

// A field inside a worker's result rows, narrowed to a vocabulary the playbook declared:
// a reader counting distinct values of a free string counts a typo as a second source.
function constrainResultField(
  schema: Record<string, unknown> | null,
  field: string,
  values: readonly string[],
): Record<string, unknown> | null {
  if (schema === null || values.length === 0) return schema;
  const properties = asRecord(schema["properties"], "roles.workers.output_schema.properties");
  const results = properties["results"];
  if (typeof results !== "object" || results === null) return schema;

  const items = (results as Record<string, unknown>)["items"];
  if (typeof items !== "object" || items === null) return schema;

  const itemProperties = (items as Record<string, unknown>)["properties"];
  if (typeof itemProperties !== "object" || itemProperties === null) return schema;

  const target = (itemProperties as Record<string, unknown>)[field];
  if (typeof target !== "object" || target === null) return schema;

  return {
    ...schema,
    properties: {
      ...properties,
      results: {
        ...(results as Record<string, unknown>),
        items: {
          ...(items as Record<string, unknown>),
          properties: { ...(itemProperties as Record<string, unknown>), [field]: { ...target, enum: [...values] } },
        },
      },
    },
  };
}

// The vocabulary a playbook declared for a section. Absent constrains nothing.
function declaredStrings(playbook: Playbook, section: string): readonly string[] {
  const declared = playbook.sections[section];
  return Array.isArray(declared) ? declared.filter((one): one is string => typeof one === "string") : [];
}

export interface SpecPaths {
  arch: string;
  playbook: string;
  config: string;
}

export interface SpecSources {
  arch: ArchSpec;
  playbook: Playbook;
  config: Config;
  prompt?: string;
}

// What each capability the arch asks for is called here. One nothing provides is
// dropped rather than fatal: the deployment loses that tool and the run goes on.
function providersOf(tools: readonly ToolSpec[]): Map<string, string[]> {
  const byCapability = new Map<string, string[]>();
  for (const tool of tools) {
    const provides = tool["provides"];
    if (typeof provides !== "string" || provides === "") continue;
    byCapability.set(provides, [...(byCapability.get(provides) ?? []), tool.id]);
  }
  return byCapability;
}

// The inverse of bindCapabilities: what the roles asked for that this deployment answers
// with nothing. Declaration order, so a ledger replays the same.
export function unboundCapabilities(roles: Roles, tools: readonly ToolSpec[]): string[] {
  const providers = providersOf(tools);
  const asking = [roles.lead, roles.critic, ...Object.values(roles.workers)];
  return [...new Set(asking.flatMap((role) => role?.needs ?? []))].filter((need) => !providers.has(need));
}

function bindCapabilities(roles: Roles, tools: readonly ToolSpec[]): Roles {
  const providers = providersOf(tools);
  const bind = (role: RoleSpec): RoleSpec => {
    const granted = role.needs.flatMap((capability) => providers.get(capability) ?? []);
    const merged = [...new Set([...role.tools, ...granted])];
    return merged.length === role.tools.length ? role : { ...role, tools: merged };
  };

  return {
    ...(roles.lead === undefined ? {} : { lead: bind(roles.lead) }),
    ...(roles.critic === undefined ? {} : { critic: bind(roles.critic) }),
    workers: Object.fromEntries(Object.entries(roles.workers).map(([id, role]) => [id, bind(role)])),
  };
}

// The one place the three layers converge, over parsed layers rather than files:
// a playbook an operator never wrote to disk converges here on the same terms.
export function assembleSpec(sources: SpecSources): RunSpec {
  const { arch, playbook, config } = sources;
  const bound = bindCapabilities(arch.roles, config.tools);
  const declared = new Set(config.tools.map((tool) => tool.id));
  const applied = applyDirectives(bound, playbook.directives, declared);
  const domains = declaredStrings(playbook, "data_domains");
  const techniques = declaredStrings(playbook, "attack_techniques");
  const roles: Roles = {
    ...applied,
    workers: Object.fromEntries(
      Object.entries(applied.workers).map(([id, role]) => [
        id,
        {
          ...role,
          output_schema: constrainResultField(
            constrainResultField(role.output_schema, "source_system", domains),
            "attack_technique",
            techniques,
          ),
        },
      ]),
    ),
  };
  const staffed = Object.keys(roles.workers).length > 0;

  // The roster and the worker-id constraint are the lead's alone. Without one
  // there is nothing to dispatch from, because the phase list already said.
  const led =
    roles.lead === undefined
      ? roles
      : {
          ...roles,
          lead: {
            ...roles.lead,
            prompt: staffed ? `${roles.lead.prompt}\n\n${roster(roles.workers)}` : roles.lead.prompt,
            output_schema: staffed ? constrainWorkerId(roles.lead.output_schema, roles.workers) : roles.lead.output_schema,
          },
        };

  return {
    ...config,
    sections: { ...config.sections, ...playbook.sections },
    arch: arch.name,
    roles: led,
    dispatch: arch.dispatch,
    digest: arch.digest,
    name: playbook.name || arch.name,
    description: playbook.description,
    use_case: playbook.use_case,
    trigger_examples: playbook.trigger_examples,
    objectives: playbook.objectives,
    scope: playbook.scope,
    phases: playbook.phases,
    narrative: playbook.narrative,
    prompt: sources.prompt ?? "",
  };
}

// What a caller may tighten about the run it is paying for, and nothing else. The
// arch is operator-authored and never uploaded, so it has no reference form.
export function withOverrides(spec: RunSpec, overrides: Record<string, unknown> | undefined): RunSpec {
  if (overrides === undefined) return spec;
  const stray = Object.keys(overrides).filter((key) => key !== "budgets" && key !== "runtime");
  if (stray.length > 0) {
    throw new SpecError(`overrides may name budgets or runtime, not ${stray.sort().join(", ")}`);
  }
  return {
    ...spec,
    budgets: positive(merge(overrides["budgets"] ?? {}, spec.budgets, "overrides.budgets"), "overrides.budgets"),
    runtime: positive(merge(overrides["runtime"] ?? {}, spec.runtime, "overrides.runtime"), "overrides.runtime"),
  };
}

export function loadArch(path: string, handled: readonly string[]): ArchSpec {
  return load(path, (text) => parseArch(text, handled), "arch");
}

// handled is the workflow's action set, which is what makes an arch declaring
// anything else a load error; owned is the sections its workflow reads.
export function buildSpec(paths: SpecPaths, handled: readonly string[], owned: Owned = NONE, prompt = ""): RunSpec {
  return assembleSpec({
    arch: loadArch(paths.arch, handled),
    config: load(paths.config, (text) => parseConfig(text, owned), "config"),
    playbook: load(paths.playbook, (text) => parsePlaybook(text, owned), "playbook"),
    prompt,
  });
}
