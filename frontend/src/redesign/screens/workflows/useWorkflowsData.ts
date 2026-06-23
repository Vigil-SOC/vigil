/* ============================================================
   Data hooks for the Workflows screen — Workflows · Agents ·
   Skills tabs. Fetch via the shared axios client (workflows,
   agents) and the dedicated skills client, then map onto the
   redesign view shapes. useEffect + local state, matching the
   rest of the redesign (see useCases.ts). REDESIGN_GAPS.md §9.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import { workflowApi, agentsApi } from '../../../services/api'
import { skillsApi } from '../../../services/skillsApi'
import {
  mapApiWorkflow,
  mapApiAgent,
  mapApiSkill,
  type ApiWorkflow,
  type ApiAgent,
} from '../../data/mappers'
import type { Workflow, AgentTemplate, Skill } from '../../data/appData'

export type Phase = 'loading' | 'ready' | 'error'

/** all available workflows (file-based + custom, merged by the backend) */
export function useWorkflows() {
  const [rows, setRows] = useState<Workflow[]>([])
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
      .then((res) => {
        if (cancelled) return
        const list = (res.data?.workflows || []) as ApiWorkflow[]
        setRows(list.map(mapApiWorkflow))
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

  return { rows, phase, error, reload }
}

/** SOC agents (built-in templates + DB-backed customs) */
export function useAgents() {
  const [rows, setRows] = useState<AgentTemplate[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    agentsApi
      .listAgents()
      .then((res) => {
        if (cancelled) return
        const list = (res.data?.agents || []) as ApiAgent[]
        setRows(list.map(mapApiAgent))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load agents')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  return { rows, phase, error, reload }
}

/** reusable skills + an optimistic active/inactive toggle persisted to the API */
export function useSkills() {
  const [rows, setRows] = useState<Skill[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    skillsApi
      .list()
      .then((list) => {
        if (cancelled) return
        setRows(list.map(mapApiSkill))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as { message?: string })?.message || 'Failed to load skills')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  // Optimistic toggle: flip locally, persist, roll back on failure.
  const toggleActive = useCallback((id: string) => {
    let next = false
    setRows((prev) =>
      prev.map((s) => {
        if (s.id !== id) return s
        next = !s.active
        return { ...s, active: next }
      })
    )
    skillsApi.update(id, { is_active: next }).catch(() => {
      setRows((prev) =>
        prev.map((s) => (s.id === id ? { ...s, active: !next } : s))
      )
    })
  }, [])

  return { rows, phase, error, reload, toggleActive }
}
