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
  /** Runs started in the last 7 days, running ones included. */
  runs7d: number
  /** Completed over finished runs in those 7 days, 0..1. Null when none has finished. */
  successRate: number | null
  successLevel: 'good' | 'fair' | 'poor' | null
  /** What starts it: "alerts", "schedule", "shadow". Empty means a person does;
   *  absent means the backend did not say. */
  triggers?: string[]
  /** False only when switched off. */
  enabled: boolean
  /** Mean cost of those 7 days' finished runs. Null when none have finished. */
  meanCostUsd: number | null
  /** Custom workflows only; file workflows omit it. */
  updatedAt?: string
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
  /** what the agent does: description, else specialization */
  does: string
  model: string | null
  modelSource: 'agent' | 'assignment' | 'default' | null
  /** Triage, Investigation, Reporting… for an assignment-sourced model */
  category: string | null
  skills: number
  changes: 'read_only' | 'asks_first' | 'on_its_own' | null
  /** null when the stats query failed server-side */
  runs7d: number | null
  /** percent 0..100, null with no runs or a failed stats query */
  successPct: number | null
  successLevel: 'good' | 'fair' | 'poor' | null
  enabled: boolean
  /** true for DB-backed forked copies (handle starts with "custom-") */
  custom: boolean
}

export interface Skill {
  id: string
  name: string
  desc: string
  /** path of the skill file when the API provides it */
  source?: string
  /** true when the directory is under the bundled library */
  bundled: boolean
  /** files in the skill folder, SKILL.md included */
  fileCount: number
}
