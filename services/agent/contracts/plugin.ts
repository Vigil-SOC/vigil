// What a plugin is: the files that tell one agent loop what kind of work this is.
// The loop, the ledger, recall, budgets, parking and stopping are the kernel's
// (core/agent.ts). A plugin says what the agent is, what it may need, which
// playbooks it can load, and what shape it must finish in. A plugin that needs
// code -- tools that write its own record, a gate before it starts, its own way
// of ending -- ships hooks beside the manifest (core/agent.ts, PluginHooks).

export interface SkillSpec {
  // What the agent names when it loads one, and the one line it reads to choose.
  name: string;
  description: string;
  // The playbook itself. Loaded only when asked for, so an unused skill costs
  // its name and description and nothing else.
  body: string;
}

// A structured answer: validated against `schema` by the loop's emission.
export interface StructuredConclusion {
  schema: Record<string, unknown>;
  // Which field is the one-paragraph summary a reader sees first.
  summary_field: string;
  // Which field holds the result refs the answer rests on. Absent means the run
  // cites nothing, and its results are not numbered.
  citations_field?: string;
}

// The run ends on prose: whatever the agent said last, once it stopped calling tools.
export interface ProseConclusion {
  schema: null;
}

export type ConclusionSpec = StructuredConclusion | ProseConclusion;

export function isStructured(conclusion: ConclusionSpec): conclusion is StructuredConclusion {
  return conclusion.schema !== null;
}

// A second look before a conclusion stands. When the answer's `field` holds one
// of `guarded`, it stands only if the reviewer upholds it and at least one cited
// ref is a real result; otherwise the run ends on `fallback`. The asymmetry is the
// point: some answers are cheap to give and expensive to give wrongly.
export interface GuardSpec {
  field: string;
  guarded: string[];
  fallback: string;
  // What the reviewer is told it is for. The reviewer never holds tools.
  reviewer_prompt: string;
}

export interface PluginManifest {
  id: string;
  description: string;
  // Who the agent is and how it works. Rendered first in the system prompt.
  system: string;
  // Capabilities, bound to this deployment's tools by their `provides` field, the
  // same binding an arch role's needs get.
  needs: string[];
  skills: SkillSpec[];
  conclusion: ConclusionSpec;
  guard?: GuardSpec;
  // Tool turns across the whole run, resumes included. Absent takes the
  // deployment's runtime.max_turns.
  max_turns?: number;
}
