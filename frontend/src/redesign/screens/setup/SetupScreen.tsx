// frontend/src/redesign/screens/setup/SetupScreen.tsx
//
// Standalone /setup screen. Step logic is design-agnostic (setupSteps +
// useSetupChecklist); this file is the presentation + the inline step panels.
import { Fragment, useEffect, useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import '../../styles.css'
import { Icon } from '../../shared/icons'
import { VigilMark } from '../../shared/VigilLogo'
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

// Per-browser "I'm done here" flag, set on "Go to dashboard". Readiness stays
// live-derived; this only lets a finished user leave /setup without it nagging.
const SETUP_DISMISSED_KEY = 'vigil.setupDismissed'
const readSetupDismissed = (): boolean => {
  try {
    return localStorage.getItem(SETUP_DISMISSED_KEY) === '1'
  } catch {
    return false
  }
}
const markSetupDismissed = (): void => {
  try {
    localStorage.setItem(SETUP_DISMISSED_KEY, '1')
  } catch {
    /* storage unavailable */
  }
}
const clearSetupDismissed = (): void => {
  try {
    localStorage.removeItem(SETUP_DISMISSED_KEY)
  } catch {
    /* storage unavailable */
  }
}

// `my-auto` (not items-center) centers vertically while staying overflow-safe:
// when an expanded step overflows the viewport, items-center would clip the top.
const Shell = ({ children }: { children: React.ReactNode }) => (
  <div className="soc-console">
    <div className="absolute inset-0 overflow-auto">
      <div className="min-h-full flex justify-center px-6 py-6">
        <div className="w-full max-w-xl my-auto">{children}</div>
      </div>
    </div>
  </div>
)

const Header = () => (
  <header className="text-center mb-6">
    <span className="inline-grid place-items-center w-12 h-12 rounded-lg bg-accent-dim text-accent-2 mb-3">
      <VigilMark className="w-6 h-6" />
    </span>
    <h1 className="text-tx text-xl font-semibold">Welcome to Vigil</h1>
    <p className="text-tx-3 text-sm mt-1">
      Just one thing to start: connect an AI provider. Everything else is
      optional and can wait.
    </p>
  </header>
)

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
      {/* ✓ = done · accent ring = required, still to do · dashed + = optional */}
      <span
        className={`grid place-items-center w-6 h-6 shrink-0 rounded-full border transition-colors duration-200 motion-reduce:transition-none ${
          step.ready
            ? 'bg-ok-dim border-ok text-ok'
            : required
              ? 'border-accent-line text-accent-2'
              : 'border-dashed border-[#39404d] text-tx-faint'
        }`}
      >
        {step.ready ? (
          <Icon name="check2" size={13} />
        ) : (
          !required && <Icon name="plus" size={12} />
        )}
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-tx text-sm font-medium">{step.label}</div>
        <div className="text-tx-3 text-xs mt-0.5">{step.description}</div>
      </div>
      {/* Button and the "done" tag share this slot (tag overlaid) and crossfade —
          no layout shift. When ready the button is inert + hidden from a11y. */}
      <div className="shrink-0 relative">
        <div
          className={`transition-opacity duration-200 motion-reduce:transition-none ${
            step.ready ? 'opacity-0 pointer-events-none' : 'opacity-100'
          }`}
          aria-hidden={step.ready}
        >
          <button
            className={`btn ${required ? 'primary' : 'ghost'}`}
            onClick={onAction}
            tabIndex={step.ready ? -1 : undefined}
          >
            {expanded ? 'Close' : required ? 'Connect' : 'Configure'}
            <Icon
              name="chevD"
              size={14}
              className={`transition-transform duration-200 ${expanded ? 'rotate-180' : ''}`}
            />
          </button>
        </div>
        <span
          className={`absolute inset-0 flex items-center justify-end whitespace-nowrap pointer-events-none text-ok text-xs font-medium transition-opacity duration-200 motion-reduce:transition-none ${
            step.ready ? 'opacity-100' : 'opacity-0'
          }`}
          aria-hidden={!step.ready}
        >
          {step.doneLabel}
        </span>
      </div>
    </div>
  )
}

const TIER_LEAD_IN: Record<string, string> = {
  recommended: 'Recommended',
  optional: 'Optional',
}

// grid 0fr→1fr animates to the content's natural height. Children mount only
// while open (lingering through the close), so each panel fetches lazily on open.
const Collapse = ({ open, children }: { open: boolean; children: React.ReactNode }) => {
  const [mounted, setMounted] = useState(open)
  useEffect(() => {
    if (open) setMounted(true)
  }, [open])
  return (
    <div
      className="grid transition-[grid-template-rows] duration-[220ms] ease-[cubic-bezier(0.4,0,0.2,1)] motion-reduce:transition-none"
      style={{ gridTemplateRows: open ? '1fr' : '0fr' }}
      onTransitionEnd={(e) => {
        if (!open && e.propertyName === 'grid-template-rows' && e.target === e.currentTarget) {
          setMounted(false)
        }
      }}
    >
      <div
        className={`overflow-hidden min-h-0 transition-opacity duration-[180ms] ${
          open ? 'opacity-100' : 'opacity-0'
        }`}
      >
        {open || mounted ? children : null}
      </div>
    </div>
  )
}

const SetupScreen = () => {
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const { steps, loading, refetch } = useSetupChecklist()
  const [activeStep, setActiveStep] = useState<SetupStepId | null>(null)

  const llmReady = steps.find((s) => s.id === 'llm-provider')?.ready ?? false

  // The dismissed flag means "user already left setup". If we're on /setup with
  // the required provider NOT ready — a fresh install, or after a reset/loss that
  // routed us here via SetupGate — that prior dismissal is stale. Clear it so
  // completing the provider doesn't instantly bounce the user out before they can
  // run the optional steps; they re-arm it by clicking "Go to dashboard".
  useEffect(() => {
    if (!loading && !llmReady) clearSetupDismissed()
  }, [loading, llmReady])

  const optionalSteps = steps.filter((s) => s.tier !== 'required')
  const requiredCount = steps.length - optionalSteps.length
  const optionalDone = optionalSteps.filter((s) => s.ready).length
  const checklistSummary = !llmReady
    ? `${requiredCount} required · ${optionalSteps.length} optional`
    : `Ready · ${optionalDone} of ${optionalSteps.length} optional done`

  // Finished (all ready) or dismissed → leave /setup. The llmReady guard avoids a
  // loop: if the provider is later lost, SetupGate sends us back and we show the wizard.
  const allReady = steps.length > 0 && steps.every((s) => s.ready)
  if (!loading && llmReady && (allReady || readSetupDismissed())) {
    return <Navigate to="/" replace />
  }

  const closeStep = () => setActiveStep(null)
  const handleSaved = () => {
    setActiveStep(null)
    refetch()
  }

  // The wizard creates with is_default:false; promote one here or the required
  // step never flips ready. Idempotent — only when nothing is default yet.
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
      // fail-open — refetch reflects the real backend state
    }
    refetch()
  }

  const handleAction = (step: ChecklistStep) =>
    setActiveStep((cur) => (cur === step.id ? null : step.id))

  const renderStepPanel = (id: SetupStepId) => {
    switch (id) {
      case 'llm-provider':
        return (
          <LlmProviderWizard
            existing={null}
            onClose={closeStep}
            onSaved={handleProviderSaved}
            showCancel={false}
          />
        )
      case 'data-source':
        return <DataSourceDialog onSaved={handleSaved} />
      case 'model-assignment':
        return <ModelAssignmentDialog onClose={closeStep} onSaved={handleSaved} />
      case 'cost-guardrails':
        return <BudgetDialog onClose={closeStep} onSaved={handleSaved} />
      case 'autonomy':
        return (
          <AutonomyDialog
            onClose={closeStep}
            onSaved={handleSaved}
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
      <SettingsCard title="Setup checklist" desc={checklistSummary}>
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
                    <div className="border-t border-line-soft pt-3 pb-1.5">
                      <span className="text-tx-2 text-xs font-medium">{TIER_LEAD_IN[step.tier]}</span>
                    </div>
                  )}
                  <div
                    className={`${!leadIn && i > 0 ? 'border-t border-line-soft' : ''} ${
                      hero || expanded ? 'bg-bg-1' : ''
                    }`}
                  >
                    <StepRow step={step} expanded={expanded} onAction={() => handleAction(step)} />
                    <Collapse open={expanded}>
                      <div className="pt-1 pb-4 pl-9 pr-2">{renderStepPanel(step.id)}</div>
                    </Collapse>
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
          onClick={() => {
            markSetupDismissed()
            navigate('/', { replace: true })
          }}
        >
          Go to dashboard
          <Icon name="arrowR" size={15} />
        </button>
      </div>
    </Shell>
  )
}

export default SetupScreen
