// frontend/src/redesign/screens/setup/SetupScreen.tsx
//
// Redesign-styled first-access setup screen, rendered on the real /setup route.
// Logic is design-agnostic (useSetupChecklist + setupSteps); presentation uses
// the redesign primitives (shared/ui, shared/icons, styles.css tokens) and
// reuses the redesign's own LlmProviderDialog for the provider step.
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import '../../styles.css'
import { Icon } from '../../shared/icons'
import { SettingsCard } from '../../shared/ui'
import LlmProviderDialog from '../settings/LlmProviderDialog'
import AutonomyDialog from './AutonomyDialog'
import BudgetDialog from './BudgetDialog'
import ModelAssignmentDialog from './ModelAssignmentDialog'
import DataSourceDialog from './DataSourceDialog'
import useSetupChecklist, { type ChecklistStep } from '../../../hooks/useSetupChecklist'
import type { SetupStepId } from '../../../setup/setupSteps'
import { useAuth } from '../../../contexts/AuthContext'
import { llmProviderApi } from '../../../services/api'

const Shell = ({ children }: { children: React.ReactNode }) => (
  <div className="soc-console">
    <div className="absolute inset-0 overflow-auto">
      <div className="min-h-full flex items-center justify-center p-6">
        <div className="w-full max-w-xl">{children}</div>
      </div>
    </div>
  </div>
)

const Header = () => (
  <header className="text-center mb-6">
    <span className="inline-grid place-items-center w-12 h-12 rounded-lg bg-accent-dim text-accent-2 mb-3">
      <Icon name="shield" size={24} />
    </span>
    <h1 className="text-tx text-xl font-semibold">Welcome to Vigil</h1>
    <p className="text-tx-3 text-sm mt-1">
      Connect an AI provider to begin — triage, investigation, and chat all run on it.
      The rest is optional and can wait.
    </p>
  </header>
)

const StepRow = ({ step, onAction }: { step: ChecklistStep; onAction: () => void }) => (
  <div className="flex items-center gap-3 py-3 border-b border-line-soft last:border-b-0">
    <span
      className={`grid place-items-center w-6 h-6 shrink-0 rounded-full border ${
        step.ready ? 'bg-ok-dim border-ok text-ok' : 'border-line text-tx-faint'
      }`}
    >
      {step.ready && <Icon name="check2" size={13} />}
    </span>
    <div className="flex-1 min-w-0">
      <div className="text-tx text-sm font-medium">{step.label}</div>
      <div className="text-tx-3 text-xs mt-0.5">{step.description}</div>
    </div>
    {!step.ready && (
      <button className={`btn shrink-0${step.required ? ' primary' : ''}`} onClick={onAction}>
        {step.required ? 'Connect' : 'Configure'}
        {!step.required && <Icon name="arrowR" size={14} />}
      </button>
    )}
  </div>
)

const SetupScreen = () => {
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const { steps, loading, refetch } = useSetupChecklist()
  const [activeStep, setActiveStep] = useState<SetupStepId | null>(null)
  const [error, setError] = useState<string | null>(null)

  const llmReady = steps.find((s) => s.id === 'llm-provider')?.ready ?? false
  const readyCount = steps.filter((s) => s.ready).length

  // The redesign's LlmProviderDialog creates with is_default:false. For
  // first-access we must guarantee a default exists, or the required step never
  // flips ready. Idempotent: only promotes when nothing is default yet.
  const closeDialog = () => setActiveStep(null)
  const handleSaved = () => {
    setActiveStep(null)
    refetch()
  }

  const handleProviderSaved = async () => {
    setActiveStep(null)
    try {
      const { data } = await llmProviderApi.list()
      const providers = data || []
      if (providers.length && !providers.some((p) => p.is_default)) {
        const target = providers.find((p) => p.is_active) ?? providers[0]
        await llmProviderApi.setDefault(target.provider_id)
      }
    } catch {
      // fail-open — refetch reflects whatever the backend actually has
    }
    refetch()
  }

  // Every step configures in a dialog layered over this screen — no navigation
  // away. Deeper tuning still lives in Settings; this covers the first run.
  const handleAction = (step: ChecklistStep) => setActiveStep(step.id)

  if (!hasPermission('settings.write')) {
    return (
      <Shell>
        <Header />
        <SettingsCard title="Setup required" desc="Administrator access needed">
          <p className="text-tx-2 text-sm">
            Vigil isn&apos;t set up yet. Ask an administrator to add an AI provider in
            Settings → AI Config.
          </p>
        </SettingsCard>
      </Shell>
    )
  }

  return (
    <Shell>
      <Header />
      {error && (
        <div className="mb-4 px-3 py-2 rounded border border-crit bg-crit-dim text-crit text-sm">
          {error}
        </div>
      )}
      <SettingsCard title="Setup checklist" desc={`${readyCount} of ${steps.length} configured`}>
        {loading ? (
          <div className="py-8 text-center text-tx-3 text-sm">Checking setup…</div>
        ) : (
          <div>
            {steps.map((step) => (
              <StepRow key={step.id} step={step} onAction={() => handleAction(step)} />
            ))}
          </div>
        )}
      </SettingsCard>
      <div className="flex justify-end mt-5">
        <button
          className="btn primary disabled:opacity-50 disabled:cursor-not-allowed"
          disabled={!llmReady}
          onClick={() => navigate('/', { replace: true })}
        >
          Go to dashboard
          <Icon name="arrowR" size={15} />
        </button>
      </div>

      {activeStep === 'llm-provider' && (
        <LlmProviderDialog
          existing={null}
          onClose={closeDialog}
          onSaved={handleProviderSaved}
          onError={setError}
        />
      )}
      {activeStep === 'autonomy' && (
        <AutonomyDialog onClose={closeDialog} onSaved={handleSaved} onError={setError} />
      )}
      {activeStep === 'cost-guardrails' && (
        <BudgetDialog onClose={closeDialog} onSaved={handleSaved} onError={setError} />
      )}
      {activeStep === 'model-assignment' && (
        <ModelAssignmentDialog onClose={closeDialog} onSaved={handleSaved} onError={setError} />
      )}
      {activeStep === 'data-source' && (
        <DataSourceDialog onClose={closeDialog} onSaved={handleSaved} />
      )}
    </Shell>
  )
}

export default SetupScreen
