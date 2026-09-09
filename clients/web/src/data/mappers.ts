import { format } from 'date-fns'
import { findingEndpoints, sourceTime, sourceTimestamp } from './findingPresentation'
import {
  MISSING_FINDING_SCORE,
  MISSING_FINDING_SEVERITY,
  type CaseRow,
  type Finding,
} from './data'
import type { Schema } from '../services/apiTypes'
import {
  prettyHandle,
  type Workflow,
  type AgentTemplate,
  type Skill,
  type Decision,
  type Outcome,
} from './appData'
import type { IconName } from '../shared/icons'
import { techniqueTactic } from './mitre'

const DASH = '—'

export type ApiCase = Schema<'CaseSchema'>

export interface ApiFinding {
  finding_id: string
  severity?: string | null
  data_source?: string
  timestamp?: string | null
  anomaly_score?: number | null
  title?: string
  description?: string
  mitre_predictions?: Record<string, number>
  status?: string
  /** open-ended on purpose: the key set varies by data source, so anything
   *  beyond the known names is carried through to `Finding.extra` */
  entity_context?: {
    hostnames?: string[]
    usernames?: string[]
    dest_ips?: string[]
    file_hashes?: string[]
    source_evidence?: unknown
    [key: string]: unknown
  }
}

export function initials(name?: string): string {
  if (!name) return DASH
  const parts = name.replace(/[._\-@]/g, ' ').trim().split(/\s+/).filter(Boolean)
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase()
  return (parts[0] || name).slice(0, 2).toUpperCase()
}

export function compactAge(iso?: string): string {
  if (!iso) return DASH
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return DASH
  const sec = Math.max(0, (Date.now() - then) / 1000)
  if (sec < 3600) return `${Math.round(sec / 60)}m`
  if (sec < 86400) return `${Math.round(sec / 3600)}h`
  return `${Math.round(sec / 86400)}d`
}

function fmt(iso: string | undefined, pattern: string): string {
  if (!iso) return DASH
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? DASH : format(d, pattern)
}

function caseStatus(s?: string): CaseRow['status'] {
  if (s === 'investigating') return 'investigating'
  if (s === 'open') return 'open'
  return 'closed' // resolved / closed / anything else
}

function casePrio(p?: string): CaseRow['prio'] {
  const v = (p || '').toLowerCase()
  if (v === 'critical' || v === 'high' || v === 'medium' || v === 'low') return v
  return 'medium'
}

export function mapApiCase(c: ApiCase): CaseRow {
  return {
    id: c.case_id || '',
    title: c.title || c.case_id || '',
    desc: c.description || '',
    status: caseStatus(c.status ?? undefined),
    prio: casePrio(c.priority ?? undefined),
    owner: initials(c.assignee ?? undefined),
    ownerName: c.assignee || 'unassigned',
    findings: c.finding_ids?.length ?? 0,
    tactic: c.mitre_techniques?.[0] || DASH,
    age: compactAge(c.created_at ?? undefined),
    sla: DASH,
    slaState: 'ok',
    updated: fmt(c.updated_at || c.created_at || undefined, 'MMM d'),
    updatedTs: epochMs(c.updated_at || c.created_at || undefined),
    createdTs: epochMs(c.created_at ?? undefined),
  }
}

function epochMs(s?: string): number | undefined {
  if (!s) return undefined
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? undefined : d.getTime()
}

function findingSev(s?: string | null): Finding['sev'] {
  const v = (s || '').toLowerCase()
  if (v === 'critical') return 'Critical'
  if (v === 'high') return 'High'
  if (v === 'medium') return 'Medium'
  if (v === 'low') return 'Low'
  return MISSING_FINDING_SEVERITY
}

export function formatFindingScore(score: number | null | undefined): string {
  return typeof score === 'number' ? score.toFixed(2) : MISSING_FINDING_SCORE
}

/** mitre_predictions is keyed by *technique* id (e.g. "T1567.002") */
function topTechnique(preds?: Record<string, number>): { tech: string; conf: number } {
  if (!preds) return { tech: DASH, conf: 0 }
  let best = ''
  let bestConf = -1
  for (const [k, v] of Object.entries(preds)) {
    if (v > bestConf) {
      best = k
      bestConf = v
    }
  }
  return best ? { tech: best, conf: Math.round(bestConf * 100) } : { tech: DASH, conf: 0 }
}

/** already surfaced as fixed columns; the rest become `extra` */
const MAPPED_ENTITY_KEYS = new Set(['hostnames', 'usernames', 'source_evidence'])

function extraEntities(ec: ApiFinding['entity_context']): Record<string, string> | undefined {
  if (!ec) return undefined
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(ec)) {
    if (MAPPED_ENTITY_KEYS.has(k) || v == null) continue
    const text = Array.isArray(v) ? v.filter(Boolean).join(', ') : String(v)
    if (text) out[k] = text
  }
  return Object.keys(out).length ? out : undefined
}

export function mapApiFinding(f: ApiFinding): Finding {
  const { tech, conf } = topTechnique(f.mitre_predictions)
  const ec = f.entity_context
  return {
    id: f.finding_id,
    title: f.title,
    ...findingEndpoints(ec),
    sev: findingSev(f.severity),
    tech,
    conf,
    tactic: techniqueTactic(tech),
    src: f.data_source || DASH,
    host: ec?.hostnames?.[0] || DASH,
    user: ec?.usernames?.[0] || DASH,
    time: sourceTime(f.timestamp),
    ts: sourceTimestamp(f.timestamp),
    score: typeof f.anomaly_score === 'number' ? f.anomaly_score : null,
    status: findingStatus(f.status),
    extra: extraEntities(ec),
  }
}

/** backend finding statuses are new / investigating / resolved */
function findingStatus(s?: string): Finding['status'] {
  if (s === 'investigating') return 'investigating'
  if (s === 'resolved' || s === 'closed') return 'closed'
  return 'open' // new / open / anything else
}

export interface ApiDecision {
  decision_id: string
  agent_id?: string
  decision_type?: string
  confidence_score?: number // 0–1
  reasoning?: string
  recommended_action?: string
  finding_id?: string
  case_id?: string
  workflow_id?: string
  timestamp?: string
  /** agree | partial | disagree — set once a human reviews */
  human_decision?: string
  feedback_timestamp?: string
  decision_metadata?: { investigation_id?: string } & Record<string, unknown>
  /** true_positive | false_positive | true_negative | false_negative | unknown */
  actual_outcome?: string
  time_saved_minutes?: number
  has_feedback?: boolean
}

/** `agent_id` is an action id from core/agents/builtins.py, already words, so
 *  the label is derived rather than mapped (#476) */
export function getAgentDisplayName(agentId?: string): string {
  if (!agentId) return DASH
  return prettyHandle(agentId)
}

/** the human verdict, NOT the backend's actual_outcome — separate axes */
function decisionOutcome(human?: string): Outcome {
  if (human === 'agree') return 'agree'
  if (human === 'disagree') return 'disagree'
  if (human === 'partial') return 'modify'
  return 'pending'
}

function decisionHuman(human?: string): string {
  if (human === 'agree') return 'Approved'
  if (human === 'disagree') return 'Rejected'
  if (human === 'partial') return 'Modified'
  return 'Pending'
}

function timeSaved(minutes?: number): string {
  if (!minutes || minutes <= 0) return DASH
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const mins = minutes % 60
  return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`
}

/** first non-empty wins */
function decisionInv(d: ApiDecision): string {
  return (
    d.workflow_id ||
    d.decision_metadata?.investigation_id ||
    d.finding_id ||
    d.case_id ||
    DASH
  )
}

export function mapApiDecision(d: ApiDecision): Decision {
  return {
    id: d.decision_id,
    agent: getAgentDisplayName(d.agent_id),
    type: d.decision_type || DASH,
    inv: decisionInv(d),
    conf:
      typeof d.confidence_score === 'number'
        ? Math.round(d.confidence_score * 100)
        : 0,
    ai: d.recommended_action || DASH,
    human: decisionHuman(d.human_decision),
    outcome: decisionOutcome(d.human_decision),
    saved: timeSaved(d.time_saved_minutes),
    time: fmt(d.timestamp, 'MMM d, HH:mm'),
    rationale: d.reasoning || '',
    // no evidence list from the backend; the detail pane hides an empty card
    evidence: [],
  }
}

export interface ApiWorkflow {
  id: string
  name?: string
  description?: string
  agents?: string[]
  tools_used?: string[]
  use_case?: string
  trigger_examples?: string[]
  source?: string
  run_kind?: string
  hunt_like?: boolean
}

/** the backend carries no presentation icon, so derive one from the name */
function workflowIcon(name: string): IconName {
  const n = name.toLowerCase()
  if (n.includes('hunt')) return 'graph'
  if (n.includes('forensic')) return 'search'
  if (n.includes('investigat')) return 'shield'
  if (n.includes('phish') || n.includes('report')) return 'doc'
  if (n.includes('incident') || n.includes('response')) return 'bolt'
  return 'flow'
}

export function mapApiWorkflow(w: ApiWorkflow): Workflow {
  // the backend often returns the slug as the name (e.g. "cloud-incident")
  const raw = w.name || w.id
  const name = raw.includes(' ') ? raw : prettyHandle(raw)
  return {
    id: w.id,
    icon: workflowIcon(raw),
    name,
    desc: w.description || '',
    agents: w.agents || [],
    cmds: w.trigger_examples || [],
    source: w.source || 'file',
    useCase: w.use_case || '',
    runKind: w.run_kind || 'compose',
    // The backend derives this from the kind, so the console never has to hold a
    // list of which kinds run the hypothesis loop. Falls back to the one kind that
    // did before the flag existed, so an older backend still reads correctly.
    huntLike: w.hunt_like ?? w.run_kind === 'hunt',
  }
}

export interface ApiAgent {
  id: string
  name?: string
  description?: string
  icon?: string
  color?: string
  specialization?: string
  recommended_tools?: string[]
}

export function mapApiAgent(a: ApiAgent): AgentTemplate {
  return {
    name: a.name || a.id,
    handle: a.id,
    spec: a.specialization || a.description || '—',
    ini: initials(a.name || a.id),
    color: a.color || 'var(--accent)',
    tools: a.recommended_tools?.length,
    custom: a.id.startsWith('custom-'),
  }
}

export interface ApiSkill {
  skill_id: string
  name: string
  description?: string | null
  category?: string
  version?: number
  is_active?: boolean
  created_by?: string | null
}

export function mapApiSkill(s: ApiSkill): Skill {
  return {
    name: s.name,
    id: s.skill_id,
    v: `v${s.version ?? 1}`,
    // 'custom' keeps the accent tag; every built-in category folds to neutral
    cat: s.category === 'custom' ? 'custom' : 'builtin',
    active: s.is_active ?? false,
    desc: s.description || '',
  }
}
