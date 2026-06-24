// frontend/src/redesign/screens/setup/SetupScreen.tsx
//
// Redesign-styled first-access setup screen, rendered on the real /setup route.
// Logic is design-agnostic (useSetupChecklist + setupSteps); presentation uses
// the redesign primitives (shared/ui, shared/icons, styles.css tokens). Every
// step configures inline — each expands its form in place (accordion), no modal.
// The provider step reuses the redesign's LlmProviderWizard body (the same flow
// Settings shows in a modal), rendered inline here.
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

// /setup is revisitable while setup is unfinished, but steps aside once the
// user is done. "Dismissed" is a per-browser localStorage flag set when they
// leave via "Go to dashboard" (guarded — storage can throw in private mode).
// Readiness stays live-derived; this is only the UI "I'm finished here" signal.
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
    /* storage unavailable — skip persistence */
  }
}

// Vertically + horizontally centered, so it reads as centered across monitor
// sizes. `my-auto` (not `items-center`) does the vertical centering to stay
// overflow-safe: when an expanded step makes the card taller than the viewport,
// auto margins collapse and the scroll container keeps the top reachable
// (plain align-items:center would clip it). Trade-off: the card re-centers as
// steps expand/collapse, so the layout shifts a bit on open — accepted here in
// favor of a consistently centered position.
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
      {/* Marker semantics: ✓ = done (positive feedback, any tier); accent ring =
          the required gate, still to do; dashed + = an optional step you can add
          anytime (an opt-in slot, not an unchecked-and-overdue box). */}
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
      {/* Right slot: the action button crossfades into a "done" status tag in the
          same spot (tag absolutely overlaid), so finishing a step resolves the
          button into a status instead of vanishing into empty space. Once ready
          the button is inert and hidden from assistive tech. */}
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

// Section lead-in shown above the first step of the 'recommended' / 'optional' tiers.
const TIER_LEAD_IN: Record<string, string> = {
  recommended: 'Recommended',
  optional: 'Optional',
}

// Smoothly animates an inline step panel open/closed. The grid 0fr→1fr trick
// transitions to the content's natural height (no fixed max-height guess); the
// child is clipped via overflow during the slide. Content mounts only while
// open and lingers through the close transition before unmounting — so each
// step's panel still fetches lazily (on open), never on setup load.
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
  const [error, setError] = useState<string | null>(null)

  const llmReady = steps.find((s) => s.id === 'llm-provider')?.ready ?? false
  // Counter copy: before the required step is met, state the composition
  // ("N required · M optional" — only one thing is mandatory), then switch to
  // optional progress. Avoids the "0 of 5" framing that read as a 5-item quota.
  const optionalSteps = steps.filter((s) => s.tier !== 'required')
  const requiredCount = steps.length - optionalSteps.length
  const optionalDone = optionalSteps.filter((s) => s.ready).length
  // Once llmReady && every optional step is done, `allReady` is true and the
  // screen redirects (below) before this renders — so there's no "all set"
  // branch here; while this is on screen at least one optional step is open.
  const checklistSummary = !llmReady
    ? `${requiredCount} required · ${optionalSteps.length} optional`
    : `Ready · ${optionalDone} of ${optionalSteps.length} optional done`

  // Once setup is finished — every step ready, or dismissed by leaving via
  // "Go to dashboard" — /setup steps aside. Guarded by llmReady so that if the
  // provider is later lost (SetupGate routes back here) the user lands on the
  // wizard to fix it rather than bouncing in a redirect loop.
  const allReady = steps.length > 0 && steps.every((s) => s.ready)
  if (!loading && llmReady && (allReady || readSetupDismissed())) {
    return <Navigate to="/" replace />
  }

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
