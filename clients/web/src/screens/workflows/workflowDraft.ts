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
  phase_id?: unknown
  agent_id?: unknown
  agent?: unknown
  name?: unknown
  purpose?: unknown
  steps?: unknown
  tools?: unknown
  expected_output?: unknown
  approval_required?: unknown
  timeout_seconds?: unknown
}

// A pasted definition can hold anything, so every field is read as the type the builder needs.
const str = (v: unknown) => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '')
const strs = (v: unknown) => (Array.isArray(v) ? v.map(str).filter(Boolean) : typeof v === 'string' && v.trim() ? [v] : [])

/** A saved workflow, an AI draft, a template or a pasted definition, as the builder holds it. */
export function draftFrom(src: { name?: unknown; description?: unknown; use_case?: unknown; trigger_examples?: unknown; phases?: LoosePhase[] | null }): BuilderDraft {
  return {
    name: str(src.name),
    description: str(src.description),
    use_case: str(src.use_case),
    trigger_examples: strs(src.trigger_examples),
    phases: (Array.isArray(src.phases) ? src.phases : []).filter((p) => p && typeof p === 'object').map((p) => ({
      key: newKey(),
      ...(str(p.phase_id) ? { phase_id: str(p.phase_id) } : {}),
      name: str(p.name),
      agent_id: str(p.agent_id) || str(p.agent),
      purpose: str(p.purpose),
      steps: strs(p.steps),
      tools: strs(p.tools),
      expected_output: str(p.expected_output),
      approval_required: p.approval_required === true,
      timeout_seconds: typeof p.timeout_seconds === 'number' && p.timeout_seconds > 0 ? p.timeout_seconds : DEFAULT_TIMEOUT,
    })),
  }
}

/**
 * Every step's phase_id, unique. A saved step keeps its own so run history lines up; a new or
 * duplicated one gets the first free `phase-N`. Left blank, the server numbers steps by position,
 * and a step put before "phase-1" would collide with it and the workflow could not run.
 */
export function phaseIds(phases: BuilderStep[]): string[] {
  const seen = new Set<string>()
  const kept = phases.map((p) => (p.phase_id && !seen.has(p.phase_id) ? (seen.add(p.phase_id), p.phase_id) : null))
  let n = 1
  return kept.map((id) => {
    if (id) return id
    while (seen.has(`phase-${n}`)) n++
    seen.add(`phase-${n}`)
    return `phase-${n}`
  })
}

/** The body of POST or PUT /workflows/custom. */
export function toPayload(d: BuilderDraft) {
  const ids = phaseIds(d.phases)
  return {
    name: d.name.trim(),
    description: d.description.trim(),
    use_case: d.use_case.trim(),
    trigger_examples: d.trigger_examples.map((t) => t.trim()).filter(Boolean),
    phases: d.phases.map((p, i) => ({
      phase_id: ids[i],
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
