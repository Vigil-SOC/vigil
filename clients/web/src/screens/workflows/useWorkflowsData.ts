import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { workflowApi, agentsApi } from '../../services/api'
import { skillsApi } from '../../services/skillsApi'
import {
  mapApiWorkflow,
  mapApiAgent,
  mapApiSkill,
  type ApiWorkflow,
  type ApiAgent,
} from '../../data/mappers'
import { prettyHandle } from '../../data/appData'
import type { Workflow, AgentTemplate, Skill } from '../../data/appData'

interface AgentGrant {
  id: string
  recommended_tools: string[]
}

/** Surfaces offered the whole skill library: Ask Vigil first, then workflows
 *  whose phase agents are granted read_skill. Ask Vigil is unconditional: a chat
 *  with no agent_id declares every built-in tool (_declare(None) in
 *  core/llm/chat_layers.py), so chat_stream adds <available_skills>. An agent id
 *  the agent list does not have is skipped. Phase tool arrays are not the grant. */
export function workflowsOffered(
  workflows: Pick<Workflow, 'name' | 'agents'>[],
  agents: AgentGrant[],
): string[] {
  const tools = new Map(agents.map((a) => [a.id, a.recommended_tools]))
  return [
    'Ask Vigil',
    ...workflows
      .filter((w) => w.agents.some((id) => (tools.get(id) ?? []).includes('read_skill')))
      .map((w) => w.name),
  ]
}

export type Phase = 'loading' | 'ready' | 'error'

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

export function useAgents() {
  const [rows, setRows] = useState<AgentTemplate[]>([])
  const [grants, setGrants] = useState<AgentGrant[]>([])
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
        setGrants(list.map((a) => ({ id: a.id, recommended_tools: a.recommended_tools ?? [] })))
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

  return { rows, grants, phase, error, reload }
}

interface AgentMeta {
  label: string
  color: string
}

const AgentRows = createContext<AgentTemplate[]>([])
export const AgentRowsProvider = AgentRows.Provider

/** Returns a resolver `(agentId) => { label, color }` for agent chips, read from
 *  the agents feed the nearest AgentRowsProvider holds. */
export function useAgentMeta(): (id: string) => AgentMeta {
  const rows = useContext(AgentRows)
  return useCallback(
    (id: string): AgentMeta => {
      const a = rows.find((r) => r.handle === id)
      return a ? { label: a.name, color: a.color } : { label: prettyHandle(id), color: 'var(--accent)' }
    },
    [rows],
  )
}

/** read-only skills list from GET /api/skills */
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

  return { rows, phase, error, reload }
}

/** The line under a model name: where the model came from. */
export function modelSource(a: Pick<AgentTemplate, 'model' | 'modelSource' | 'category'>): string | null {
  if (!a.model) return null
  if (a.modelSource === 'agent') return 'Set for this agent'
  if (a.modelSource === 'assignment' && a.category) return `${a.category.charAt(0).toUpperCase()}${a.category.slice(1).replace(/_/g, ' ')} default`
  return 'Default'
}
