/* ============================================================
   Map real backend API responses (snake_case, partial) onto the
   redesign's view shapes (Finding / CaseRow). The view shapes
   carry richer fields than the API returns, so anything the API
   omits falls back gracefully (em-dash / derived / neutral state).
   See REDESIGN_GAPS.md §9.
   ============================================================ */
import { format } from 'date-fns'
import type { CaseRow, Finding } from './data'
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

/** raw case object as returned by GET /cases/ (fields are best-effort/optional) */
export interface ApiCase {
  case_id: string
  title?: string
  description?: string
  status?: string
  priority?: string
  assignee?: string
  finding_ids?: string[]
  /** GET /cases/{id} returns full finding objects (include_findings=True) */
  findings?: ApiFinding[]
  finding_count?: number
  created_at?: string
  updated_at?: string
  mitre_techniques?: string[]
  primary_tactic?: string
  tactic?: string
  sla?: string
  sla_remaining?: string
  sla_state?: string
  timeline?: Array<{ event?: string; timestamp?: string }>
}

/** raw finding object as returned by GET /findings/ */
export interface ApiFinding {
  finding_id: string
  severity?: string
  data_source?: string
  timestamp?: string
  anomaly_score?: number
  title?: string
  description?: string
  mitre_predictions?: Record<string, number>
  status?: string
  /** host / user / IP entities extracted by the backend */
  entity_context?: {
    hostnames?: string[]
    usernames?: string[]
    dest_ips?: string[]
    file_hashes?: string[]
  }
}

/* ---------------- shared helpers ---------------- */

/** "j.reyes" / "Jane Reyes" → "JR"; falls back to em-dash */
export function initials(name?: string): string {
  if (!name) return DASH
  const parts = name.replace(/[._\-@]/g, ' ').trim().split(/\s+/).filter(Boolean)
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase()
  return (parts[0] || name).slice(0, 2).toUpperCase()
}

/** compact relative age: "12m" / "5h" / "3d" */
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

/* ---------------- cases ---------------- */

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

function slaState(s?: string): CaseRow['slaState'] {
  const v = (s || '').toLowerCase()
  if (v === 'breached') return 'danger'
  if (v === 'danger' || v === 'warn' || v === 'ok') return v as CaseRow['slaState']
  return 'ok'
}

export function mapApiCase(c: ApiCase): CaseRow {
  return {
    id: c.case_id,
    title: c.title || c.case_id,
    desc: c.description || '',
    status: caseStatus(c.status),
    prio: casePrio(c.priority),
    owner: initials(c.assignee),
    ownerName: c.assignee || 'unassigned',
    findings: c.finding_count ?? c.finding_ids?.length ?? 0,
    tactic: c.mitre_techniques?.[0] || c.primary_tactic || c.tactic || DASH,
    age: compactAge(c.created_at),
    sla: c.sla || c.sla_remaining || DASH,
    slaState: slaState(c.sla_state),
    updated: fmt(c.updated_at || c.created_at, 'MMM d'),
    updatedTs: epochMs(c.updated_at || c.created_at),
    createdTs: epochMs(c.created_at),
  }
}

/** parse an ISO timestamp to epoch ms, or undefined if missing/invalid */
function epochMs(s?: string): number | undefined {
  if (!s) return undefined
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? undefined : d.getTime()
}

/* ---------------- findings ---------------- */

function findingSev(s?: string): Finding['sev'] {
  const v = (s || '').toLowerCase()
  if (v === 'critical') return 'Critical'
  if (v === 'high') return 'High'
  if (v === 'low') return 'Low'
  return 'Medium'
}

/**
 * The top-confidence MITRE prediction. The backend's mitre_predictions map is
 * keyed by *technique* IDs (e.g. "T1567.002"), so this yields the technique and
 * its confidence.
 */
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

export function mapApiFinding(f: ApiFinding): Finding {
  const { tech, conf } = topTechnique(f.mitre_predictions)
  const ec = f.entity_context
  return {
    id: f.finding_id,
    sev: findingSev(f.severity),
    tech,
    conf,
    tactic: techniqueTactic(tech),
    src: f.data_source || DASH,
    host: ec?.hostnames?.[0] || DASH,
    user: ec?.usernames?.[0] || DASH,
    time: fmt(f.timestamp, 'MMM d, HH:mm'),
    ts: epochMs(f.timestamp),
    score: typeof f.anomaly_score === 'number' ? f.anomaly_score : 0,
    status: findingStatus(f.status),
  }
}

/** backend finding statuses are new / investigating / resolved */
function findingStatus(s?: string): Finding['status'] {
  if (s === 'investigating') return 'investigating'
  if (s === 'resolved' || s === 'closed') return 'closed'
  return 'open' // new / open / anything else
}

/* ---------------- AI decisions ---------------- */

/** raw decision as returned by GET /ai/decisions (fields best-effort/optional) */
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

/** agent_id → display name (ported from pages/AIDecisions.tsx:186-195) */
const AGENT_NAMES: Record<string, string> = {
  triage: 'Triage',
  investigation: 'Investigation',
  threat_hunter: 'Threat Hunter',
  correlation: 'Correlation',
  auto_responder: 'Auto-Response',
  reporting: 'Reporting',
  mitre_analyst: 'MITRE',
  forensics: 'Forensics',
  threat_intel: 'Threat Intel',
  compliance: 'Compliance',
  malware_analyst: 'Malware',
  network_analyst: 'Network',
  orchestrator: 'Orchestrator',
}

export function getAgentDisplayName(agentId?: string): string {
  if (!agentId) return DASH
  return AGENT_NAMES[agentId] || agentId
}

/**
 * The redesign's outcome chip collapses the *human verdict*
 * (agree/partial/disagree), NOT the backend's actual_outcome
 * (true/false-positive) — those are separate axes (see DECISIONS_WIRING.md §3).
 */
function decisionOutcome(human?: string): Outcome {
  if (human === 'agree') return 'agree'
  if (human === 'disagree') return 'disagree'
  if (human === 'partial') return 'modify'
  return 'pending'
}

/** human-readable label for the verdict column */
function decisionHuman(human?: string): string {
  if (human === 'agree') return 'Approved'
  if (human === 'disagree') return 'Rejected'
  if (human === 'partial') return 'Modified'
  return 'Pending'
}

/** minutes → "45m" / "2h" / "2h 30m"; em-dash when absent or zero */
function timeSaved(minutes?: number): string {
  if (!minutes || minutes <= 0) return DASH
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const mins = minutes % 60
  return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`
}

/** which id to surface in the "Investigation" column — first non-empty wins */
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
    // backend returns no evidence list — the detail pane hides the card when empty
    evidence: [],
  }
}

/* ---------------- workflows ---------------- */

/** raw workflow as returned by GET /workflows (WorkflowDefinition.to_dict) */
export interface ApiWorkflow {
  id: string
  name?: string
  description?: string
  agents?: string[]
  tools_used?: string[]
  use_case?: string
  trigger_examples?: string[]
  source?: string
}

/**
 * The backend doesn't carry a presentation icon, so pick one from keywords in
 * the workflow name (purely cosmetic — falls back to the generic flow glyph).
 */
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
  // The backend often returns the slug as the name (e.g. "cloud-incident");
  // present it as Title Case when it has no spaces.
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
  }
}

/* ---------------- agents ---------------- */

/** raw agent as returned by GET /agents (agent_manager.get_agent_list) */
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

/* ---------------- skills ---------------- */

/** raw skill as returned by GET /api/skills (SkillResponse) */
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
    // 'custom' keeps the accent tag styling; every built-in category folds
    // into the neutral "built-in" tag.
    cat: s.category === 'custom' ? 'custom' : 'builtin',
    active: s.is_active ?? false,
    desc: s.description || '',
  }
}
