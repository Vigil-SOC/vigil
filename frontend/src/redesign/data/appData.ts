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

/**
 * Display label + dot color for each built-in agent, keyed by the canonical
 * agent handle the backend returns in a workflow's `agents` array (lowercase
 * ids like "mitre_analyst"). Colors mirror services/soc_agents.py so the
 * sequence chips match the Agents tab.
 */
export const AGENT_META: Record<string, { label: string; color: string }> = {
  triage: { label: 'Triage', color: '#FF6B6B' },
  investigator: { label: 'Investigator', color: '#4ECDC4' },
  threat_hunter: { label: 'Threat Hunter', color: '#95E1D3' },
  correlator: { label: 'Correlator', color: '#F38181' },
  responder: { label: 'Responder', color: '#FF8B94' },
  reporter: { label: 'Reporter', color: '#A8E6CF' },
  mitre_analyst: { label: 'MITRE Analyst', color: '#FFD3B6' },
  forensics: { label: 'Forensics', color: '#FFAAA5' },
  threat_intel: { label: 'Threat Intel', color: '#B4A7D6' },
  compliance: { label: 'Compliance', color: '#C7CEEA' },
  malware_analyst: { label: 'Malware Analyst', color: '#FF6B9D' },
  network_analyst: { label: 'Network Analyst', color: '#56CCF2' },
  auto_responder: { label: 'Auto-Response', color: '#FF6B6B' },
}

/** "mitre_analyst" → "Mitre Analyst" — fallback for unknown/custom handles. */
export function prettyHandle(handle: string): string {
  return handle
    .replace(/[._-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
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
