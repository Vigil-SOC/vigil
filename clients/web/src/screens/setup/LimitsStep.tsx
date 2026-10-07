import { useEffect, useState } from 'react'
import { configApi } from '../../services/api'
import { fmtCost } from '../../shared/cost'
import { Icon } from '../../shared/icons'
import { ConfirmDialog } from '../../shared/ui'
import {
  matchesProfile,
  ORCHESTRATOR_DEFAULTS,
  raisesLimit,
  stripOrchestratorProfiles,
  type InvestigationProfile,
  type InvestigationProfiles,
  type InvestigationProfileValues,
  type OrchestratorConfig,
} from '../settings/useSettings'
import { errorText } from './errorText'

function fmtRuntime(seconds: number): string {
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`
}

// 0 means no cap in the stored config
const capped = (fmt: (n: number) => string) => (n: number) => (n === 0 ? 'Unlimited' : fmt(n))

const PROFILE_FIELDS: {
  key: keyof InvestigationProfileValues
  label: string
  fmt: (n: number) => string
}[] = [
  { key: 'max_cost_per_investigation', label: 'Max cost per investigation', fmt: capped((n) => fmtCost(n)) },
  { key: 'max_iterations_per_agent', label: 'Max iterations per agent', fmt: capped(String) },
  { key: 'max_runtime_per_investigation', label: 'Max runtime per investigation', fmt: capped(fmtRuntime) },
  { key: 'max_concurrent_agents', label: 'Max concurrent agents', fmt: capped(String) },
  { key: 'max_total_hourly_cost', label: 'Max hourly cost', fmt: capped((n) => `${fmtCost(n)} / h`) },
]

type LoadPhase = 'loading' | 'ready' | 'error'

interface Approval {
  enabled: boolean
  environment_wins: boolean
}

export default function LimitsStep() {
  const [profiles, setProfiles] = useState<InvestigationProfiles>({})
  // The one current copy of the stored config; every save writes the whole of it.
  const [config, setConfig] = useState<OrchestratorConfig>(ORCHESTRATOR_DEFAULTS)
  const [pendingLimits, setPendingLimits] = useState<OrchestratorConfig | null>(null)
  const [savingLimits, setSavingLimits] = useState(false)
  const [profilesPhase, setProfilesPhase] = useState<LoadPhase>('loading')
  const [approval, setApproval] = useState<Approval>({ enabled: false, environment_wins: false })
  const [approvalPhase, setApprovalPhase] = useState<LoadPhase>('loading')
  const [confirmAct, setConfirmAct] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    configApi
      .getOrchestrator()
      .then(({ data }) => {
        if (!live) return
        const row = (data ?? {}) as Partial<OrchestratorConfig> & { profiles?: InvestigationProfiles }
        setProfiles(row.profiles ?? {})
        setConfig({ ...ORCHESTRATOR_DEFAULTS, ...stripOrchestratorProfiles(row) })
        setProfilesPhase('ready')
      })
      .catch(() => {
        if (live) setProfilesPhase('error')
      })
    configApi
      .getForceManualApproval()
      .then(({ data }) => {
        if (!live) return
        const row = data as Partial<Approval>
        setApproval({
          enabled: Boolean(row.enabled),
          environment_wins: Boolean(row.environment_wins),
        })
        setApprovalPhase('ready')
      })
      .catch(() => {
        if (live) setApprovalPhase('error')
      })
    return () => {
      live = false
    }
  }, [])

  const saveLimits = async (next: OrchestratorConfig) => {
    setError(null)
    setSavingLimits(true)
    try {
      await configApi.setOrchestrator(next)
      setConfig(next)
    } catch (err) {
      setError(errorText(err, 'Could not save the limits.'))
    } finally {
      setSavingLimits(false)
    }
  }

  const pickProfile = (profile: InvestigationProfile) => {
    const next = { ...config, ...profile.values }
    if (raisesLimit(config, next)) setPendingLimits(next)
    else saveLimits(next)
  }

  const saveApproval = async (enabled: boolean) => {
    setError(null)
    try {
      const { data } = await configApi.setForceManualApproval(enabled)
      const row = data as Partial<Approval>
      setApproval({
        enabled: Boolean(row.enabled),
        environment_wins: Boolean(row.environment_wins),
      })
    } catch (err) {
      setError(errorText(err, 'Could not save the response mode.'))
    }
  }

  const selectAssist = () => {
    if (approval.enabled) return
    saveApproval(true)
  }

  const selectAct = () => {
    if (approval.environment_wins) {
      saveApproval(false)
      return
    }
    if (!approval.enabled) return
    setConfirmAct(true)
  }

  if (profilesPhase === 'loading' || approvalPhase === 'loading') {
    return <p className="text-tx-3 text-sm">Loading limits…</p>
  }

  const actOn = approvalPhase === 'ready' && !approval.enabled
  const entries = Object.entries(profiles)
  const activeKey = entries.find(([, profile]) => matchesProfile(config, profile.values))?.[0] ?? null
  // Custom shows the saved values, since no profile describes them
  const shown = activeKey === null ? config : profiles[activeKey].values

  return (
    <div className="flex flex-col gap-4">
      {profilesPhase === 'error' ? (
        <p className="text-sm text-high">Could not read investigation profiles.</p>
      ) : (
        <div className="flex flex-col gap-3">
          <div
            role="radiogroup"
            aria-label="Limits profile"
            className="settings-grid-2"
            style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 150px), 1fr))' }}
          >
            {entries.map(([key, profile]) => (
              <ProfileCard
                key={key}
                profile={profile}
                selected={key === activeKey}
                disabled={savingLimits}
                onPick={() => pickProfile(profile)}
              />
            ))}
          </div>
          {activeKey === null && (
            <div className="settings-banner info">
              <Icon name="info" size={14} />
              <span>Custom limits in effect. Your saved values match no profile.</span>
            </div>
          )}
          <div>
            <h4 className="text-xs font-semibold text-tx-2 mb-1">Default case limits</h4>
            <dl className="grid gap-0.5">
              {PROFILE_FIELDS.map((field) => (
                <div key={field.key} className="flex justify-between gap-3 text-xs">
                  <dt className="text-tx-3">{field.label}</dt>
                  <dd className="text-tx-2">{field.fmt(shown[field.key])}</dd>
                </div>
              ))}
            </dl>
          </div>
        </div>
      )}

      {approvalPhase === 'error' ? (
        <p className="text-sm text-high">Could not read the response mode.</p>
      ) : (
        <>
          {approval.environment_wins && (
            <div className="settings-banner info">
              <Icon name="info" size={14} />
              <span>The environment wins. Act cannot be saved.</span>
            </div>
          )}
          {error && <p className="text-sm text-high">{error}</p>}
          <div className="settings-grid-2" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 150px), 1fr))' }}>
            <button
              type="button"
              aria-pressed={!actOn}
              onClick={selectAssist}
              className={`card card-sq text-left p-3.5 ${actOn ? '' : 'border-accent-line bg-[var(--accent-dim)]'}`}
            >
              <div className="text-[13px] font-semibold text-tx">Assist</div>
              <span className="text-xs text-tx-3">Force manual approval before a response runs.</span>
            </button>
            <button
              type="button"
              aria-pressed={actOn}
              onClick={selectAct}
              className={`card card-sq text-left p-3.5 ${actOn ? 'border-accent-line bg-[var(--accent-dim)]' : ''}`}
            >
              <div className="flex flex-wrap items-center gap-2 text-[13px] font-semibold text-tx">
                Act <span className="chip">Recommended</span>
              </div>
              <span className="text-xs text-tx-3">Let autonomous response proceed without forcing approval.</span>
            </button>
          </div>
        </>
      )}

      <ConfirmDialog
        open={pendingLimits !== null}
        title="Raise investigation limits?"
        body="This increases a cost, runtime, or concurrency cap. Confirm to save."
        confirmLabel="Save"
        danger={false}
        onConfirm={() => {
          const next = pendingLimits
          setPendingLimits(null)
          if (next) saveLimits(next)
        }}
        onClose={() => setPendingLimits(null)}
      />
      <ConfirmDialog
        open={confirmAct}
        title="Switch to Act?"
        body="Act stops forcing manual approval, so autonomous response can proceed on its own."
        confirmLabel="Save"
        danger={false}
        onConfirm={() => {
          setConfirmAct(false)
          saveApproval(false)
        }}
        onClose={() => setConfirmAct(false)}
      />
    </div>
  )
}

function ProfileCard({
  profile,
  selected,
  disabled,
  onPick,
}: {
  profile: InvestigationProfile
  selected: boolean
  disabled: boolean
  onPick: () => void
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      disabled={disabled}
      onClick={onPick}
      className={`card card-sq text-left p-3.5 flex flex-col items-start justify-start gap-1 ${selected ? 'border-accent-line bg-[var(--accent-dim)]' : ''}`}
    >
      <div className="flex flex-wrap items-center gap-2 text-[13px] font-semibold text-tx">
        {profile.label}
        {profile.recommended && <span className="chip">Recommended</span>}
      </div>
      <span className="text-xs text-tx-3">
        {profile.values.max_concurrent_agents} agents · {fmtCost(profile.values.max_cost_per_investigation)} / case
      </span>
    </button>
  )
}
