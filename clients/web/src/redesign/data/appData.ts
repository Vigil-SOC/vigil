/* ============================================================
   Shared view-model types + agent display metadata for the
   Workflows, AI Decisions, Agents and Skills screens. Screens
   fetch real data via services/api and map it into these shapes
   (see data/mappers.ts).
   ============================================================ */
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
}

// Agent label + dot color used to be mirrored here as AGENT_META; it now comes
// from GET /agents at runtime via useAgentMeta (#482), so built-in colors/labels
// can't drift from the backend. prettyHandle stays as the offline fallback.

/** "mitre_mapping" → "MITRE Mapping" — labels every agent and action id. */
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

// The Decision view shape is now produced by mapApiDecision (mappers.ts) and
// fed to DecisionsScreen via the useDecisions hooks — the old static mock list
// and decStats() were removed when the screen was wired to aiDecisionsApi.

/* ---- Agents (built-in templates) ---- */
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

/* ---- Skills (reusable capabilities) ---- */
export interface Skill {
  name: string
  id: string
  v: string
  cat: 'custom' | 'builtin'
  active: boolean
  desc: string
}
