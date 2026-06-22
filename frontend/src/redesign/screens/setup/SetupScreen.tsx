// frontend/src/redesign/screens/setup/SetupScreen.tsx
//
// Redesign-styled first-access setup screen, rendered on the real /setup route.
// Logic is design-agnostic (useSetupChecklist + setupSteps); presentation uses
// the redesign primitives (shared/ui, shared/icons, styles.css tokens). Every
// step configures inline — each expands its form in place (accordion), no modal.
// The provider step reuses the redesign's LlmProviderWizard body (the same flow
// Settings shows in a modal), rendered inline here.
import { Fragment, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import '../../styles.css'
import { Icon } from '../../shared/icons'
import { SettingsCard } from '../../shared/ui'
import { LlmProviderWizard } from '../settings/LlmProviderDialog'
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

// One checklist row. Ready steps show a ✓ and no action. Not-ready steps toggle
// their inline config panel ("Configure"/"Connect" ⇄ "Close"). The required
// provider step is emphasized: accent status dot + primary button.
const StepRow = ({
  step,
  expanded,
  onAction,
}: {
  step: ChecklistStep
  expanded: boolean
  onAction: () => void
}) => {
  const required = step.tier === 'required'
  return (
    <div className="flex items-center gap-3 py-3">
      <span
        className={`grid place-items-center w-6 h-6 shrink-0 rounded-full border ${
          step.ready
            ? 'bg-ok-dim border-ok text-ok'
            : required
              ? 'border-accent-line text-accent-2'
              : 'border-line text-tx-faint'
        }`}
      >
        {step.ready && <Icon name="check2" size={13} />}
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-tx text-sm font-medium">{step.label}</div>
        <div className="text-tx-3 text-xs mt-0.5">{step.description}</div>
      </div>
      {!step.ready && (
        <button className={`btn shrink-0 ${required ? 'primary' : 'ghost'}`} onClick={onAction}>
          {expanded ? 'Close' : required ? 'Connect' : 'Configure'}
          <Icon name="chevD" size={14} className={expanded ? 'rotate-180' : ''} />
        </button>
      )}
    </div>
  )
}

// Section lead-in shown above the first step of the 'recommended' / 'optional' tiers.
const TIER_LEAD_IN: Record<string, { label: string; hint: string }> = {
  recommended: { label: 'Recommended', hint: 'a SOC needs telemetry to triage' },
  optional: { label: 'Optional', hint: 'do more with Vigil, anytime' },
}

const SetupScreen = () => {
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const { steps, loading, refetch } = useSetupChecklist()
  const [activeStep, setActiveStep] = useState<SetupStepId | null>(null)
  const [error, setError] = useState<string | null>(null)

  const llmReady = steps.find((s) => s.id === 'llm-provider')?.ready ?? false
  const readyCount = steps.filter((s) => s.ready).length

  const closeStep = () => setActiveStep(null)
  const handleSaved = () => {
    setActiveStep(null)
    refetch()
  }

  // The redesign's LlmProviderDialog creates with is_default:false. For
  // first-access we must guarantee a default exists, or the required step never
  // flips ready. Idempotent: only promotes when nothing is default yet.
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

  // Each step's action toggles its inline panel open/closed.
  const handleAction = (step: ChecklistStep) =>
    setActiveStep((cur) => (cur === step.id ? null : step.id))

  // The inline body for a step. The provider step uses handleProviderSaved (to
  // promote a default); the rest collapse + refetch on save.
  const renderStepPanel = (id: SetupStepId) => {
    switch (id) {
      case 'llm-provider':
        return (
          <LlmProviderWizard
            existing={null}
            onClose={closeStep}
            onSaved={handleProviderSaved}
            onError={setError}
            showCancel={false}
          />
        )
      case 'data-source':
        return <DataSourceDialog onSaved={handleSaved} />
      case 'model-assignment':
        return <ModelAssignmentDialog onClose={closeStep} onSaved={handleSaved} onError={setError} />
      case 'cost-guardrails':
        return <BudgetDialog onClose={closeStep} onSaved={handleSaved} onError={setError} />
      case 'autonomy':
        return (
          <AutonomyDialog
            onClose={closeStep}
            onSaved={handleSaved}
            onError={setError}
            onConfigureBudget={() => setActiveStep('cost-guardrails')}
          />
        )
      default:
        return null
    }
  }

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
            {steps.map((step, i) => {
              const expanded = activeStep === step.id
              const prevTier = i > 0 ? steps[i - 1].tier : null
              const leadIn = step.tier !== 'required' && step.tier !== prevTier
              const hero = step.tier === 'required'
              return (
                <Fragment key={step.id}>
                  {leadIn && (
                    <div className="flex items-baseline gap-2 border-t border-line-soft pt-3 pb-1.5">
                      <span className="text-tx-2 text-xs font-medium">
                        {TIER_LEAD_IN[step.tier].label}
                      </span>
                      <span className="text-tx-faint text-[11px]">{TIER_LEAD_IN[step.tier].hint}</span>
                    </div>
                  )}
                  <div
                    className={`${!leadIn && i > 0 ? 'border-t border-line-soft' : ''} ${
                      hero || expanded ? 'bg-bg-1' : ''
                    }`}
                  >
                    <StepRow step={step} expanded={expanded} onAction={() => handleAction(step)} />
                    {expanded && (
                      <div className="pt-1 pb-4 pl-9 pr-2">{renderStepPanel(step.id)}</div>
                    )}
                  </div>
                </Fragment>
              )
            })}
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
    </Shell>
  )
}

export default SetupScreen
