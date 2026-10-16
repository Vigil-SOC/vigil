// What a plugin is: what the agent is, what it may need, the playbooks it loads and the
// shape it finishes in. Loop, ledger, recall and parking are the kernel's (core/agent.ts).

// A plugin that needs code (its own record's tools, a gate before it starts, its own
// ending) ships hooks beside the manifest: PluginHooks in core/agent.ts.

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
  // Which field holds what the run could not settle, in its own words: its
  // declared gaps. Required of a plugin whose record is required.
  open_questions_field?: string;
}

// The run ends on prose: whatever the agent said last, once it stopped calling tools.
export interface ProseConclusion {
  schema: null;
}

export type ConclusionSpec = StructuredConclusion | ProseConclusion;

export function isStructured(conclusion: ConclusionSpec): conclusion is StructuredConclusion {
  return conclusion.schema !== null;
}

// A second look: a `guarded` value in `field` stands only if the reviewer upholds it over
// a cited real result, else the run ends on `fallback`. Some answers are costly to give wrongly.
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
  // Experimental (core/guards.ts), honoured only with VIGIL_GUARDS=on: the reader's
  // prompt, and what the deciding agent is told, in this plugin's words. Not `guard`,
  // which is the kernel reviewer's second look at a conclusion.
  quarantine?: { reader: string; guidance: string };
  // Tool turns across the whole run, resumes included. Absent takes the
  // deployment's runtime.max_turns.
  max_turns?: number;
  // The hypothesis record (core/record.ts): available offers its tools, required also
  // holds a guarded answer to it and its gaps, off offers nothing. Absent is available.
  record?: RecordMode;
  // The results a run should gather before it may stop unasked. A run that stops
  // with fewer, and turns left, is asked once to keep going or confirm it is done.
  min_results?: number;
  // How a structured answer is given: ask (the default) stops and is asked, a model call of
  // its own; tool has the agent call conclude in the turn it decides, ending the run.
  finish_with?: "ask" | "tool";
}

export const RECORD_MODES = ["off", "available", "required"] as const;
export type RecordMode = (typeof RECORD_MODES)[number];

export function recordModeOf(plugin: PluginManifest): RecordMode {
  return plugin.record ?? "available";
}
