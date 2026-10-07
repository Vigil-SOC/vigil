import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import '../../../../../docs/design/console/tokens/tokens.css'
import '../../styles.css'
import '../../shell/shell.css'
import './setup.css'
import { useColorScheme } from '../../contexts/ColorSchemeContext'
import { Icon } from '../../shared/icons'
import { SettingsCard } from '../../shared/ui'
import { useAuth } from '../../contexts/AuthContext'
import { ToastProvider, useToast } from '../../shell/toast'
import SetupProviderStep from './SetupProviderStep'
import DataSourceDialog from './DataSourceDialog'
import { DEMO_ENV_NOTICE, turnOnDemoMode } from './demoMode'
import SystemChecksStep from './SystemChecksStep'
import WorkflowsStep from './WorkflowsStep'
import LimitsStep from './LimitsStep'
import SummaryStep from './SummaryStep'
import { Rail, TopBar } from './SetupChrome'
import {
  SETUP_STEP_COUNT,
  markSetupDismissed,
  readSetupProgress,
  writeSetupProgress,
  type SetupProgress,
} from './setupDismissed'

// the done page follows the five numbered steps and is never counted in "n of 5"
const STEPS = ['checks', 'data', 'ai', 'workflows', 'limits', 'done'] as const

type StepId = (typeof STEPS)[number]

const DONE = STEPS.length - 1

interface StepCopy {
  rail: string
  sub: string
  title: string
  desc: string
}

const STEP_COPY: Record<StepId, StepCopy> = {
  checks: {
    rail: 'Before you start',
    sub: 'System checks',
    title: 'Welcome to Vigil',
    desc: 'Vigil investigates your security alerts with AI agents and asks you before it changes anything. Setup takes about 15 minutes, and your progress is saved if you leave.',
  },
  data: {
    rail: 'Connect your data',
    sub: 'At least one alert source',
    title: 'Connect your data',
    desc: 'Pick where your alerts live. Vigil reads alerts from it and asks before it changes anything there. You can add more sources later.',
  },
  ai: {
    rail: 'Choose where AI runs',
    sub: 'A model provider and key',
    title: 'Choose where AI runs',
    desc: 'Agents and chat need a language model. Pick where it runs. You can choose a different model for each agent later in Settings.',
  },
  workflows: {
    rail: 'Agents and workflows',
    sub: 'What runs, and what may act alone',
    title: 'Choose what Vigil does on its own',
    desc: 'Each workflow tests possible explanations for an alert, gathers evidence for and against each one, and asks before any change. Turn off anything you do not want.',
  },
  limits: {
    rail: 'Limits and alerts',
    sub: 'Spend and notifications',
    title: 'Set limits and where Vigil reaches you',
    desc: 'Limits stop runaway cost. Notifications tell you when a decision needs your attention. Both can be changed later in Settings.',
  },
  // the done page draws its own head, cards and footer (SummaryStep)
  done: { rail: '', sub: '', title: '', desc: '' },
}

const RAIL = STEPS.slice(0, DONE).map((id) => ({
  title: STEP_COPY[id].rail,
  sub: STEP_COPY[id].sub,
}))

function stepPanel(
  id: StepId,
  onAdvance: () => void,
  onRoutable: () => void,
) {
  switch (id) {
    case 'checks':
      return <SystemChecksStep />
    case 'data':
      return <DataSourceDialog onAdvance={onAdvance} />
    case 'ai':
      return <SetupProviderStep onRoutable={onRoutable} />
    case 'workflows':
      return <WorkflowsStep />
    case 'limits':
      return <LimitsStep />
    case 'done':
      return null
    default: {
      const _exhaustive: never = id
      return _exhaustive
    }
  }
}

const Shell = ({ children }: { children: React.ReactNode }) => {
  const { scheme } = useColorScheme()
  return (
    <div
      className={`soc-console su-root ${scheme === 'light' ? 'vg-light' : 'vg-dark'}`}
      data-theme={scheme}
    >
      <ToastProvider>{children}</ToastProvider>
    </div>
  )
}

const SetupWizard = () => {
  const navigate = useNavigate()
  const { notify } = useToast()
  const { hasPermission } = useAuth()
  const [progress, setProgress] = useState<SetupProgress>(readSetupProgress)
  const [index, setIndex] = useState(progress.furthest - 1)
  const [demoBusy, setDemoBusy] = useState(false)
  const step = STEPS[index]
  const copy = STEP_COPY[step]
  const done = step === 'done'
  const last = index === DONE - 1

  const dismiss = () => {
    markSetupDismissed()
    navigate('/', { replace: true })
  }

  const save = (next: SetupProgress) => {
    writeSetupProgress(next)
    setProgress(next)
  }

  const advance = () => {
    const n = index + 1
    save({
      furthest: Math.max(progress.furthest, Math.min(n + 1, SETUP_STEP_COUNT)),
      passed: [...new Set([...progress.passed, n])],
    })
    setIndex(n)
  }

  // step 3 counts as passed once any provider can route, however the person got there
  const markRoutable = () => {
    if (!progress.passed.includes(3)) save({ ...progress, passed: [...progress.passed, 3] })
  }

  const finishLater = () => {
    writeSetupProgress({ ...progress, furthest: Math.max(progress.furthest, index + 1) })
    dismiss()
  }

  // epic decision 7, guard 20: a server-set DEMO_MODE=false cannot be overridden from here
  const lookAroundWithDemo = async () => {
    setDemoBusy(true)
    try {
      if ((await turnOnDemoMode()) === 'environment') notify('err', DEMO_ENV_NOTICE)
      else dismiss()
    } catch {
      notify('err', 'Demo data could not be turned on')
    } finally {
      setDemoBusy(false)
    }
  }

  const panel = stepPanel(step, advance, markRoutable)

  if (!hasPermission('settings.write')) {
    return (
      <>
        <TopBar />
        <div className="su-body">
          <aside className="su-rail" aria-hidden="true" />
          <div className="su-main">
            <div className="su-scroll">
              <div className="su-col">
                <SettingsCard title="Setup" desc="Administrator access needed">
                  <p className="text-tx-2 text-sm">
                    Ask an administrator to connect an AI provider and data sources. You can open
                    the console in the meantime.
                  </p>
                  <div className="flex justify-end mt-4">
                    <button className="btn primary" onClick={dismiss}>
                      Continue to console
                      <Icon name="arrowR" size={15} />
                    </button>
                  </div>
                </SettingsCard>
              </div>
            </div>
          </div>
        </div>
      </>
    )
  }

  return (
    <>
      <TopBar />
      <div className="su-body">
        <Rail
          steps={RAIL}
          active={done ? -1 : index}
          passed={(n) => done || progress.passed.includes(n)}
          onPick={setIndex}
          onDemo={lookAroundWithDemo}
          demoBusy={demoBusy}
        />
        <div className="su-main">
          {done ? (
            <SummaryStep onChange={(target) => setIndex(STEPS.indexOf(target))} />
          ) : (
            <>
              <div className="su-scroll">
                <div className="su-col">
                  <div className="su-head">
                    <span className="su-eyebrow">{`Step ${index + 1} of ${SETUP_STEP_COUNT}`}</span>
                    <h1>{copy.title}</h1>
                    <p>{copy.desc}</p>
                  </div>
                  {panel}
                </div>
              </div>
              <footer className="su-foot">
                <span className="su-foot-note">
                  {`Step ${index + 1} of ${SETUP_STEP_COUNT} · your progress is saved`}
                </span>
                {index > 0 && (
                  <button className="btn ghost" onClick={() => setIndex(index - 1)}>
                    Back
                  </button>
                )}
                <button className="btn ghost" onClick={finishLater}>
                  Save and finish later
                </button>
                <button className="btn primary" onClick={advance}>
                  {last ? 'Finish setup' : 'Continue'}
                  <Icon name="arrowR" size={15} />
                </button>
              </footer>
            </>
          )}
        </div>
      </div>
    </>
  )
}

const SetupScreen = () => (
  <Shell>
    <SetupWizard />
  </Shell>
)

export default SetupScreen
