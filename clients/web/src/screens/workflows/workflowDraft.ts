// The workflow the builder holds while a person edits it, and the checks it
// must pass before it is saved. Pure, so the rules are tested without a screen.
import type { CatalogTool } from './toolCatalog'

export interface BuilderStep {
  /** stable while editing; never sent */
  key: string
  /** kept from a saved workflow so a run's history still lines up */
  phase_id?: string
  name: string
  agent_id: string
  purpose: string
  steps: string[]
  tools: string[]
  expected_output: string
  approval_required: boolean
  timeout_seconds: number
}

export interface BuilderDraft {
  name: string
  description: string
  use_case: string
  trigger_examples: string[]
  phases: BuilderStep[]
}

export interface BuilderAgent {
  id: string
  name: string
  description: string
  color: string | null
  recommended_tools: string[]
  enabled: boolean
}

export interface Check {
  level: 'error' | 'warn'
  text: string
  /** the step it is about; none for the workflow as a whole */
  step?: string
}

export const DEFAULT_TIMEOUT = 300
export const TIMEOUTS = [60, 120, 300, 600, 900, 1800]

let seq = 0
export const newKey = () => `s${Date.now().toString(36)}${(seq++).toString(36)}`

export function blankStep(agent?: BuilderAgent, tools?: string[]): BuilderStep {
  return {
    key: newKey(),
    name: agent ? agent.name.replace(/ agent$/i, '') : '',
    agent_id: agent?.id ?? '',
    purpose: '',
    steps: [],
    tools: tools ?? agent?.recommended_tools.filter((t) => t !== 'read_skill') ?? [],
    expected_output: '',
    approval_required: false,
    timeout_seconds: DEFAULT_TIMEOUT,
  }
}

export const EMPTY_DRAFT: BuilderDraft = { name: '', description: '', use_case: '', trigger_examples: [], phases: [] }

interface LoosePhase {
  phase_id?: string
  agent_id?: string
  agent?: string
  name?: string
  purpose?: string
  steps?: string[]
  tools?: string[]
  expected_output?: string
  approval_required?: boolean
  timeout_seconds?: number | null
}

/** A saved workflow, an AI draft or a template, as the builder holds it. */
export function draftFrom(src: { name?: string; description?: string; use_case?: string; trigger_examples?: string[]; phases?: LoosePhase[] | null }): BuilderDraft {
  return {
    name: src.name ?? '',
    description: src.description ?? '',
    use_case: src.use_case ?? '',
    trigger_examples: [...(src.trigger_examples ?? [])],
    phases: (src.phases ?? []).map((p) => ({
      key: newKey(),
      ...(p.phase_id ? { phase_id: p.phase_id } : {}),
      name: p.name ?? '',
      agent_id: p.agent_id || p.agent || '',
      purpose: p.purpose ?? '',
      steps: [...(p.steps ?? [])],
      tools: [...(p.tools ?? [])],
      expected_output: p.expected_output ?? '',
      approval_required: !!p.approval_required,
      timeout_seconds: p.timeout_seconds || DEFAULT_TIMEOUT,
    })),
  }
}

/** The body of POST or PUT /workflows/custom. */
export function toPayload(d: BuilderDraft) {
  return {
    name: d.name.trim(),
    description: d.description.trim(),
    use_case: d.use_case.trim(),
    trigger_examples: d.trigger_examples.map((t) => t.trim()).filter(Boolean),
    phases: d.phases.map((p, i) => ({
      ...(p.phase_id ? { phase_id: p.phase_id } : {}),
      order: i + 1,
      agent_id: p.agent_id,
      name: p.name.trim(),
      purpose: p.purpose.trim(),
      steps: p.steps.map((s) => s.trim()).filter(Boolean),
      tools: [...p.tools],
      expected_output: p.expected_output.trim(),
      timeout_seconds: p.timeout_seconds,
      approval_required: p.approval_required,
    })),
  }
}

export const sameDraft = (a: BuilderDraft, b: BuilderDraft) => JSON.stringify(toPayload(a)) === JSON.stringify(toPayload(b))

const stepLabel = (p: BuilderStep, i: number) => `Step ${i + 1}${p.name.trim() ? ` (${p.name.trim()})` : ''}`

/** Errors stop the save; warnings are said and the save goes ahead. */
export function checkDraft(
  d: BuilderDraft,
  agents: BuilderAgent[] | null,
  tools: Map<string, CatalogTool> | null,
): Check[] {
  const out: Check[] = []
  if (!d.name.trim()) out.push({ level: 'error', text: 'Give the workflow a name.' })
  if (!d.description.trim()) out.push({ level: 'error', text: 'Say what the workflow does, in one line under its name.' })
  if (d.phases.length === 0) out.push({ level: 'error', text: 'Add at least one step.' })
  const byId = new Map((agents ?? []).map((a) => [a.id, a]))
  d.phases.forEach((p, i) => {
    const label = stepLabel(p, i)
    if (!p.name.trim()) out.push({ level: 'error', text: `Step ${i + 1} needs a name.`, step: p.key })
    if (!p.agent_id) out.push({ level: 'error', text: `${label} needs an agent to run it.`, step: p.key })
    else if (agents && !byId.has(p.agent_id)) out.push({ level: 'error', text: `${label}: no agent called “${p.agent_id}” exists.`, step: p.key })
    else if (byId.get(p.agent_id)?.enabled === false) out.push({ level: 'warn', text: `${label}: ${byId.get(p.agent_id)!.name} is turned off, so Run is refused until it is on.`, step: p.key })
    if (!p.purpose.trim() && p.steps.every((s) => !s.trim())) out.push({ level: 'warn', text: `${label} has no instructions; the agent only has its own prompt to go on.`, step: p.key })
    if (tools) {
      const missing = p.tools.filter((t) => !tools.has(t))
      if (missing.length) out.push({ level: 'warn', text: `${label}: ${missing.join(', ')} ${missing.length === 1 ? 'is' : 'are'} not connected here, so the step runs without ${missing.length === 1 ? 'it' : 'them'}.`, step: p.key })
      const acts = p.tools.filter((t) => tools.get(t)?.changes === 'on_its_own')
      if (acts.length && !p.approval_required) out.push({ level: 'warn', text: `${label} can change things (${acts.join(', ')}) without asking. Consider turning on approval.`, step: p.key })
    }
  })
  return out
}
