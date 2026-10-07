import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getAllIntegrations } from '../../config/integrations'
import { budgetsApi, configApi, llmProviderApi, workflowApi } from '../../services/api'
import { Icon, type IconName } from '../../shared/icons'
import { markConsoleTourSeen } from '../../shell/consoleTourSeen'
import { matchesProfile, type InvestigationProfiles } from '../settings/useSettings'
import { periodBesideCeiling } from './ceilingPeriod'
import { markSetupDismissed } from './setupDismissed'

export type SummaryTarget = 'data' | 'ai' | 'workflows' | 'limits'

type Cell = { phase: 'loading' } | { phase: 'ready'; text: string } | { phase: 'error' }

type SetupSteps = Awaited<ReturnType<typeof configApi.getSetupSteps>>['data']['steps']
type Steps = { phase: 'loading' | 'error' } | { phase: 'ready'; steps: SetupSteps }

const ROWS: { key: string; label: string; target: SummaryTarget }[] = [
  { key: 'data', label: 'Data', target: 'data' },
  { key: 'ai', label: 'AI models', target: 'ai' },
  { key: 'workflows', label: 'Workflows', target: 'workflows' },
  { key: 'autonomy', label: 'On their own', target: 'workflows' },
  { key: 'limits', label: 'Limits', target: 'limits' },
  { key: 'notify', label: 'Notifications', target: 'limits' },
]

const NEXT: { icon: IconName; title: string; sub: string; to: string }[] = [
  { icon: 'play', title: 'Take the 1-minute tour', sub: 'Where decisions, chat and search live', to: '' },
  { icon: 'link', title: 'Connect more sources', sub: 'Each source you add closes a blind spot', to: '/settings?section=integrations' },
  { icon: 'flow', title: 'Watch a hunt run', sub: 'See how Vigil tests explanations, step by step', to: '/workflows' },
]

const plural = (n: number) => `${n} optional step${n === 1 ? '' : 's'}`

/** Each row reads its own API, so one failing leaves the others standing. */
export default function SummaryStep({ onChange }: { onChange: (target: SummaryTarget) => void }) {
  const navigate = useNavigate()
  const [cells, setCells] = useState<Record<string, Cell>>({})
  const [setup, setSetup] = useState<Steps>({ phase: 'loading' })

  useEffect(() => {
    let live = true
    const orchestrator = configApi.getOrchestrator()
    const read = (key: string, load: () => Promise<string>) => {
      load()
        .then((text) => live && setCells((prev) => ({ ...prev, [key]: { phase: 'ready', text } })))
        .catch(() => live && setCells((prev) => ({ ...prev, [key]: { phase: 'error' } })))
    }

    // one read feeds the Notifications row, the footer and the Next card
    configApi
      .getSetupSteps()
      .then(({ data }) => live && setSetup({ phase: 'ready', steps: data.steps ?? [] }))
      .catch(() => live && setSetup({ phase: 'error' }))

    read('data', async () => {
      const { data } = await configApi.getIntegrations()
      const ids = (data?.enabled_integrations ?? []) as string[]
      const catalog = new Map(getAllIntegrations().map((i) => [i.id, i.name]))
      return ids.length ? ids.map((id) => catalog.get(id) ?? id).join(', ') : 'Nothing connected yet'
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
      const [{ data }, quota] = await Promise.all([
        orchestrator,
        // the ceiling is a bonus on this row: an unreadable gateway leaves it off
        budgetsApi.getQuota().catch(() => null),
      ])
      const profiles = (data?.profiles ?? {}) as InvestigationProfiles
      const match = Object.values(profiles).find((p) => matchesProfile(data, p.values))
      const label = match?.label ?? 'Custom'
      const ceiling = quota?.data?.quota?.budgets?.find(
        (b) => b.max_limit > 0 && periodBesideCeiling(b.reset_duration) === null,
      )
      return ceiling ? `${label} · $${ceiling.max_limit.toLocaleString('en-US')} a month` : label
    })

    return () => {
      live = false
    }
  }, [])

  const notify = setup.phase === 'ready' ? setup.steps.find((s) => s.id === 'notify') : undefined
  const cellFor = (key: string): Cell => {
    if (key !== 'notify') return cells[key] ?? { phase: 'loading' }
    if (setup.phase !== 'ready') return { phase: setup.phase }
    return { phase: 'ready', text: notify?.done ? 'Slack' : 'Not set' }
  }

  const left = setup.phase === 'ready' ? setup.steps.filter((s) => !s.done).length : 0

  const leave = (to: string, state?: { startTour: true }) => {
    markSetupDismissed()
    navigate(to, { replace: true, state })
  }
  const goHome = () => {
    markConsoleTourSeen()
    leave('/')
  }
  const takeTour = () => leave('/', { startTour: true })

  return (
    <>
      <div className="su-scroll">
        <div className="su-col">
          <div className="su-done-head">
            <span className="su-done-tile" aria-hidden="true">
              <Icon name="check" size={30} />
            </span>
            <div className="su-head">
              <span className="su-eyebrow good">Setup complete</span>
              <h1>Vigil is ready</h1>
              <p>
                Triage starts on new alerts as they are collected. The first cases appear on Home,
                and anything that needs you shows up in Needs your attention.
              </p>
            </div>
          </div>

          <section className="card card-sq settings-card">
            <div className="card-h">
              <div className="settings-card-head">
                <h3>What you set up</h3>
                <p>Change any of it now, or later in Settings.</p>
              </div>
            </div>
            <div className="card-b su-sum">
              {ROWS.map(({ key, label, target }) => {
                const cell = cellFor(key)
                return (
                  <div key={key} className="su-sum-row">
                    <span className="su-sum-k">{label}</span>
                    <span
                      className={`su-sum-v${cell.phase === 'error' ? ' err' : cell.phase === 'loading' ? ' wait' : ''}`}
                    >
                      {cell.phase === 'ready' ? cell.text : cell.phase === 'error' ? 'Could not read this.' : 'Loading…'}
                    </span>
                    <button
                      type="button"
                      className="btn"
                      aria-label={`Change ${label}`}
                      onClick={() => onChange(target)}
                    >
                      Change
                    </button>
                  </div>
                )
              })}
            </div>
          </section>

          <section className="card card-sq settings-card">
            <div className="card-h">
              <div className="settings-card-head">
                <h3>Next</h3>
                {left > 0 && (
                  <p>Setup in the profile menu keeps track of what is left: {plural(left)}.</p>
                )}
              </div>
            </div>
            <div className="card-b">
              <div className="su-choices three su-next">
                {NEXT.map(({ icon, title, sub, to }, i) => (
                  <button
                    key={title}
                    type="button"
                    className={`su-choice${i === 0 ? ' on' : ''}`}
                    onClick={i === 0 ? takeTour : () => leave(to)}
                  >
                    <span className="su-choice-top">
                      <span className="su-choice-icon">
                        <Icon name={icon} size={16} />
                      </span>
                    </span>
                    <span className="su-choice-t">{title}</span>
                    <span className="su-choice-s">{sub}</span>
                  </button>
                ))}
              </div>
            </div>
          </section>
        </div>
      </div>
      <footer className="su-foot">
        <span className="su-foot-note">
          Setup complete{left > 0 && ` · ${plural(left)} left in Setup`}
        </span>
        <button className="btn" onClick={takeTour}>
          <Icon name="play" size={15} />
          Take the 1-minute tour
        </button>
        <button className="btn primary" onClick={goHome}>
          Go to Home
        </button>
      </footer>
    </>
  )
}
