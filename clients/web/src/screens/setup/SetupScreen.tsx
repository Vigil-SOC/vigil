import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import '../../styles.css'
import { Icon } from '../../shared/icons'
import { VigilMark } from '../../shared/VigilLogo'
import { SettingsCard } from '../../shared/ui'
import { useAuth } from '../../contexts/AuthContext'
import SetupProviderStep from './SetupProviderStep'
import DataSourceDialog from './DataSourceDialog'
import SystemChecksStep from './SystemChecksStep'
import WorkflowsStep from './WorkflowsStep'
import LimitsStep from './LimitsStep'
import { markSetupDismissed } from './setupDismissed'

const STEPS = ['checks', 'data', 'ai', 'workflows', 'limits', 'summary'] as const

type StepId = (typeof STEPS)[number]

const STEP_COPY: Record<StepId, { title: string; desc: string }> = {
  checks: {
    title: 'System checks',
    desc: 'API health, the storage backend, and whether a provider can route.',
  },
  data: {
    title: 'Connect data',
    desc: 'A SIEM or EDR so Vigil has alerts to triage.',
  },
  ai: {
    title: 'Where AI runs',
    desc: 'A local model keeps data on site. A hosted one sends it out.',
  },
  workflows: {
    title: 'Workflows',
    desc: 'Playbooks already on this install. Nothing here is turned on or off.',
  },
  limits: {
    title: 'Limits and autonomy',
    desc: 'Profiles are the default case limits. Assist or Act is the only setting this step saves.',
  },
  summary: {
    title: 'Summary',
    desc: 'The console works from here. Reopen this pass from the account menu.',
  },
}

const Shell = ({ children }: { children: React.ReactNode }) => (
  <div className="soc-console">
    <div className="absolute inset-0 overflow-auto">
      <div className="min-h-full flex justify-center px-6 py-6">
        <div className="w-full max-w-xl my-auto">{children}</div>
      </div>
    </div>
  </div>
)

function stepPanel(id: StepId, onAdvance: () => void) {
  switch (id) {
    case 'checks':
      return <SystemChecksStep />
    case 'data':
      return <DataSourceDialog onSaved={onAdvance} />
    case 'ai':
      return <SetupProviderStep onSaved={onAdvance} />
    case 'workflows':
      return <WorkflowsStep />
    case 'limits':
      return <LimitsStep />
    case 'summary':
      return (
        <p className="text-tx-2 text-sm">
          You can use the console without a provider. Setup stays on the account menu.
        </p>
      )
    default: {
      const _exhaustive: never = id
      return _exhaustive
    }
  }
}

const SetupScreen = () => {
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const [index, setIndex] = useState(0)
  const step = STEPS[index]
  const copy = STEP_COPY[step]
  const last = index === STEPS.length - 1

  const dismiss = () => {
    markSetupDismissed()
    navigate('/', { replace: true })
  }

  const advance = () => {
    if (last) dismiss()
    else setIndex((current) => current + 1)
  }

  if (!hasPermission('settings.write')) {
    return (
      <Shell>
        <header className="text-center mb-6">
          <h1 className="text-tx text-xl font-semibold">Welcome to Vigil</h1>
        </header>
        <SettingsCard title="Setup" desc="Administrator access needed">
          <p className="text-tx-2 text-sm">
            Ask an administrator to connect an AI provider and data sources. You can open the
            console in the meantime.
          </p>
          <div className="flex justify-end mt-4">
            <button className="btn primary" onClick={dismiss}>
              Continue to console
              <Icon name="arrowR" size={15} />
            </button>
          </div>
        </SettingsCard>
      </Shell>
    )
  }

  return (
    <Shell>
      <header className="text-center mb-6">
        <span className="inline-grid place-items-center w-12 h-12 rounded-lg bg-accent-dim text-accent-2 mb-3">
          <VigilMark className="w-6 h-6" />
        </span>
        <h1 className="text-tx text-xl font-semibold">Welcome to Vigil</h1>
        <p className="text-tx-3 text-sm mt-1">
          One pass over this install. Every step can be skipped.
        </p>
        <button className="btn ghost mt-3" onClick={dismiss}>
          Skip setup
        </button>
      </header>
      <SettingsCard title={copy.title} desc={`${copy.desc} · ${index + 1} of ${STEPS.length}`}>
        {stepPanel(step, advance)}
      </SettingsCard>
      <div className="flex justify-end gap-2 mt-5">
        <button className="btn ghost" onClick={advance}>
          Skip
        </button>
        <button className="btn primary" onClick={advance}>
          {last ? 'Go to console' : 'Continue'}
          <Icon name="arrowR" size={15} />
        </button>
      </div>
    </Shell>
  )
}

export default SetupScreen
