import type { IconName } from '../shared/icons'

export interface Workflow {
  id: string
  icon: IconName
  name: string
  desc: string
  agents: string[]
  cmds: string[]
  /** "file" (built-in, read-only) or "custom" (DB-backed, editable/deletable) */
  source: string
  useCase: string
  /** "hunt" runs the hypothesis loop and is bounded by turns; the rest walk phases. */
  runKind: string
  /** Whether runKind drives the hypothesis loop — "hunt" and "root_cause" both do.
   *  Derived by the backend from its own set, so this stays true of a kind added
   *  there without a change here. What the Run dialog gates its hunt fields on. */
  huntLike: boolean
}

// AGENT_META was mirrored here until #482 moved it to GET /agents, so built-in
// colors/labels can't drift from the backend. prettyHandle is the fallback.

/** "mitre_mapping" → "MITRE Mapping" */
export function prettyHandle(handle: string): string {
  return handle
    .replace(/[._-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .replace(/\bMitre\b/g, 'MITRE')
    .trim()
}

export type Outcome = 'agree' | 'disagree' | 'modify' | 'pending'

export interface Decision {
  id: string
  agent: string
  type: string
  inv: string
  conf: number
  ai: string
  human: string
  outcome: Outcome
  saved: string
  time: string
  rationale: string
  evidence: string[]
}

export interface AgentTemplate {
  name: string
  handle: string
  spec: string
  ini: string
  color: string
  /** count of recommended tools; undefined when the list endpoint omits it */
  tools?: number
  /** true for DB-backed forked copies (handle starts with "custom-") */
  custom: boolean
}

export interface Skill {
  id: string
  name: string
  desc: string
  /** path of the skill file when the API provides it */
  source?: string
}
