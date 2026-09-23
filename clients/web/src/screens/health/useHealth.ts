import { useCallback, useEffect, useState } from 'react'
import { findingsApi, workflowApi } from '../../services/api'
import type { ApiFinding, ApiWorkflow } from '../../data/mappers'

export type Phase = 'loading' | 'ready' | 'error'

/** `other` catches a status the console does not know, so every run still lands in a bucket. */
export const RUN_STATUSES = ['completed', 'failed', 'cancelled', 'running', 'paused', 'other'] as const
export type RunStatus = (typeof RUN_STATUSES)[number]

/** How many recent runs are read per workflow — the route caps at 200. */
export const RUNS_PER_WORKFLOW = 200

export interface RunKindOutcomes {
  runKind: string
  workflows: number
  total: number
  byStatus: Record<RunStatus, number>
}

interface RunSummary {
  workflow_id?: string
  status?: string
}

interface RunsResponse {
  runs?: RunSummary[]
}

function emptyStatuses(): Record<RunStatus, number> {
  return { completed: 0, failed: 0, cancelled: 0, running: 0, paused: 0, other: 0 }
}

function toRunStatus(s: string | undefined): RunStatus {
  return s && (RUN_STATUSES as readonly string[]).includes(s) ? (s as RunStatus) : 'other'
}

/** Join recent runs onto their workflow's declared run_kind and bucket by status. */
export function bucketRunOutcomes(
  workflows: ApiWorkflow[],
  runsByWorkflow: Record<string, RunSummary[]>,
): RunKindOutcomes[] {
  const kinds = new Map<string, RunKindOutcomes>()
  for (const wf of workflows) {
    const runKind = wf.run_kind || 'compose'
    const entry = kinds.get(runKind) ?? { runKind, workflows: 0, total: 0, byStatus: emptyStatuses() }
    entry.workflows += 1
    for (const run of runsByWorkflow[wf.id] ?? []) {
      entry.total += 1
      entry.byStatus[toRunStatus(run.status)] += 1
    }
    kinds.set(runKind, entry)
  }
  return [...kinds.values()].sort((a, b) => b.total - a.total || a.runKind.localeCompare(b.runKind))
}

/** There is no all-runs endpoint and none is added: list workflows, then the
 *  recent runs of each, and join in the browser. Bounded by RUNS_PER_WORKFLOW. */
export function useRunOutcomes() {
  const [rows, setRows] = useState<RunKindOutcomes[]>([])
  /** workflows whose run listing failed; their runs are missing from the counts */
  const [unread, setUnread] = useState<string[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    workflowApi
      .listAll()
      .then(async (res) => {
        const workflows = (res.data?.workflows || []) as ApiWorkflow[]
        // one workflow's runs failing (removed since listing, say) must not blank the rest
        const settled = await Promise.allSettled(
          workflows.map((wf) =>
            workflowApi
              .listRuns(wf.id, { limit: RUNS_PER_WORKFLOW })
              .then((r) => ((r.data as RunsResponse)?.runs || []) as RunSummary[]),
          ),
        )
        if (cancelled) return
        const runsByWorkflow: Record<string, RunSummary[]> = {}
        const failed: string[] = []
        workflows.forEach((wf, i) => {
          const s = settled[i]
          if (s.status === 'fulfilled') runsByWorkflow[wf.id] = s.value
          else failed.push(wf.name || wf.id)
        })
        if (workflows.length > 0 && failed.length === workflows.length) {
          setError('Failed to load workflow runs')
          setPhase('error')
          return
        }
        setRows(bucketRunOutcomes(workflows, runsByWorkflow))
        setUnread(failed)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load workflows')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  return { rows, unread, phase, error, reload }
}

export const PROBE_OUTCOMES = ['hit', 'miss', 'silent'] as const
export type ProbeOutcome = (typeof PROBE_OUTCOMES)[number]

/** Three probes a day for a week is 21 rows; the route caps `limit` at 1000. */
export const PROBE_ROW_LIMIT = 100
export const PROBE_TALLY_DAYS = 7

/** `entity_context.probe` as `services/daemon/probes.py` writes it; `score`
 *  lands an hour after the row is created and is never rewritten. */
export interface ProbeBlock {
  name: string
  expected: { severity: string[]; recommended_action: string[] }
  score?: {
    outcome: ProbeOutcome
    verdict: { severity?: string | null; recommended_action?: string | null; confidence?: number | null } | null
    time_to_verdict_s: number | null
    scored_at: string
  }
}

export interface ProbeSummary {
  name: string
  expected: ProbeBlock['expected']
  /** the newest row has no score yet; the daemon scores a row an hour after creation */
  awaiting: boolean
  /** score of the newest scored row, which may be older than an awaiting one */
  latest: NonNullable<ProbeBlock['score']> | null
}

export interface ProbeScores {
  probes: ProbeSummary[]
  tally: Record<ProbeOutcome, number>
}

function probeBlock(f: ApiFinding): ProbeBlock | null {
  const p = f.entity_context?.probe as ProbeBlock | undefined
  return p && typeof p === 'object' && typeof p.name === 'string' ? p : null
}

const ts = (f: ApiFinding) => (f.timestamp ? Date.parse(f.timestamp) : NaN)

/** Group probe rows by name: newest row decides "awaiting", newest scored row
 *  is the result shown. The tally counts scored rows from the last week. */
export function summarizeProbes(rows: ApiFinding[], now: number = Date.now()): ProbeScores {
  const since = now - PROBE_TALLY_DAYS * 86_400_000
  const tally: Record<ProbeOutcome, number> = { hit: 0, miss: 0, silent: 0 }
  const byName = new Map<string, ProbeSummary>()
  // the route already sorts newest first; sorting again keeps this independent of that
  const sorted = [...rows].sort((a, b) => (ts(b) || 0) - (ts(a) || 0))
  for (const row of sorted) {
    const probe = probeBlock(row)
    if (!probe) continue
    const score = probe.score && (PROBE_OUTCOMES as readonly string[]).includes(probe.score.outcome) ? probe.score : undefined
    let entry = byName.get(probe.name)
    if (!entry) {
      const expected = {
        severity: Array.isArray(probe.expected?.severity) ? probe.expected.severity : [],
        recommended_action: Array.isArray(probe.expected?.recommended_action) ? probe.expected.recommended_action : [],
      }
      entry = { name: probe.name, expected, awaiting: !score, latest: null }
      byName.set(probe.name, entry)
    }
    if (score && !entry.latest) entry.latest = score
    if (score && ts(row) >= since) tally[score.outcome] += 1
  }
  return { probes: [...byName.values()].sort((a, b) => a.name.localeCompare(b.name)), tally }
}

/** Probe rows are ordinary findings with `data_source = "probe"`; one read covers a week. */
export function useProbeScores() {
  const [data, setData] = useState<ProbeScores>({ probes: [], tally: { hit: 0, miss: 0, silent: 0 } })
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    findingsApi
      .getAll({ data_source: 'probe', limit: PROBE_ROW_LIMIT })
      .then((res) => {
        if (cancelled) return
        setData(summarizeProbes(((res.data as { findings?: ApiFinding[] })?.findings || [])))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load probe findings')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  return { ...data, phase, error, reload }
}
