import { useCallback, useEffect, useState } from 'react'
import { workflowApi } from '../../services/api'
import type { ApiWorkflow } from '../../data/mappers'

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
