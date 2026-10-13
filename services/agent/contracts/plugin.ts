// What a plugin is: what the agent is, what it may need, which playbooks it loads and
// the shape it finishes in. No control flow: loop, ledger, recall and parking are the kernel's.

export interface SkillSpec {
  // What the agent names when it loads one, and the one line it reads to choose.
  name: string;
  description: string;
  // The playbook itself. Loaded only when asked for, so an unused skill costs
  // its name and description and nothing else.
  body: string;
}

export interface ConclusionSpec {
  // JSON schema for the answer the run ends on. Validated by the loop's emission.
  schema: Record<string, unknown>;
  // Which field is the one-paragraph summary a reader sees first.
  summary_field: string;
  // Which field holds the step refs the answer rests on.
  citations_field: string;
}

// A second look: a `guarded` value in `field` stands only if the reviewer upholds it over
// a cited real step, else the run ends on `fallback`. Some answers are costly to give wrongly.
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
  // Tool turns across the whole run, resumes included.
  max_turns: number;
}
