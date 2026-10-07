import { useEffect, useState } from 'react'
import { configApi, llmProviderApi, workflowApi } from '../../services/api'
import { matchesProfile, type InvestigationProfiles } from '../settings/useSettings'

export type SummaryTarget = 'data' | 'ai' | 'workflows' | 'limits'

type Cell = { phase: 'loading' } | { phase: 'ready'; text: string } | { phase: 'error' }

const ROWS: { key: string; label: string; target: SummaryTarget }[] = [
  { key: 'data', label: 'Data', target: 'data' },
  { key: 'ai', label: 'AI', target: 'ai' },
  { key: 'workflows', label: 'Workflows', target: 'workflows' },
  { key: 'autonomy', label: 'On their own', target: 'workflows' },
  { key: 'limits', label: 'Limits', target: 'limits' },
]

/** Each row reads its own API, so one failing leaves the others standing. */
export default function SummaryStep({ onChange }: { onChange: (target: SummaryTarget) => void }) {
  const [cells, setCells] = useState<Record<string, Cell>>({})

  useEffect(() => {
    let live = true
    const orchestrator = configApi.getOrchestrator()
    const read = (key: string, load: () => Promise<string>) => {
      load()
        .then((text) => live && setCells((prev) => ({ ...prev, [key]: { phase: 'ready', text } })))
        .catch(() => live && setCells((prev) => ({ ...prev, [key]: { phase: 'error' } })))
    }

    read('data', async () => {
      const { data } = await configApi.getIntegrations()
      const names = (data?.enabled_integrations ?? []) as string[]
      return names.length ? names.join(', ') : 'Nothing connected yet'
    })
    read('ai', async () => {
      const { data } = await llmProviderApi.list()
      const provider = (data ?? []).find((p) => p.is_default)
      if (!provider) return 'No provider'
      return provider.default_model ? `${provider.name} · ${provider.default_model}` : provider.name
    })
    read('workflows', async () => {
      const [list, orch] = await Promise.all([workflowApi.listAll(), orchestrator])
      const workflows = (list.data?.workflows ?? []) as { enabled?: boolean }[]
      const on = workflows.filter((w) => w.enabled !== false).length
      const auto = orch.data?.enabled
        ? 'investigates new alerts automatically'
        : 'starts only when asked'
      return `${on} of ${workflows.length} on · ${auto}`
    })
    read('autonomy', async () => {
      const { data } = await configApi.getAutonomy()
      return data.force_manual_approval
        ? 'Assist · asks before changes'
        : 'Act · reversible changes on its own'
    })
    read('limits', async () => {
      const { data } = await orchestrator
      const profiles = (data?.profiles ?? {}) as InvestigationProfiles
      const match = Object.values(profiles).find((p) => matchesProfile(data, p.values))
      return match?.label ?? 'Custom'
    })

    return () => {
      live = false
    }
  }, [])

  return (
    <div>
      {ROWS.map(({ key, label, target }, i) => {
        const cell = cells[key] ?? { phase: 'loading' }
        return (
          <div
            key={key}
            className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2.5 text-sm"
            style={i ? { borderTop: '1px solid var(--line-soft)' } : undefined}
          >
            <span className="w-full sm:w-24 shrink-0 text-xs font-semibold text-tx-2">{label}</span>
            <span
              className={`flex-1 min-w-0 break-words ${cell.phase === 'error' ? 'text-high' : cell.phase === 'loading' ? 'text-tx-3' : 'text-tx'}`}
            >
              {cell.phase === 'ready' ? cell.text : cell.phase === 'error' ? 'Could not read this.' : 'Loading…'}
            </span>
            <button
              type="button"
              className="btn shrink-0"
              aria-label={`Change ${label}`}
              onClick={() => onChange(target)}
            >
              Change
            </button>
          </div>
        )
      })}
    </div>
  )
}
