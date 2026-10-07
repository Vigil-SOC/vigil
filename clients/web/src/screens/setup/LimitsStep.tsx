import { useEffect, useState } from 'react'
import { configApi } from '../../services/api'
import { fmtCost } from '../../shared/cost'
import { Icon } from '../../shared/icons'
import { ConfirmDialog, SettingsCard } from '../../shared/ui'
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
import ChoiceCard from './ChoiceCard'
import { errorText } from './errorText'
import MonthlyCeiling from './MonthlyCeiling'
import SlackRoute from './SlackRoute'

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

export default function LimitsStep() {
  const [profiles, setProfiles] = useState<InvestigationProfiles>({})
  // The one current copy of the stored config; every save writes the whole of it.
  const [config, setConfig] = useState<OrchestratorConfig>(ORCHESTRATOR_DEFAULTS)
  const [pendingLimits, setPendingLimits] = useState<OrchestratorConfig | null>(null)
  const [savingLimits, setSavingLimits] = useState(false)
  const [showLimits, setShowLimits] = useState(false)
  const [profilesPhase, setProfilesPhase] = useState<LoadPhase>('loading')
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

  const entries = Object.entries(profiles)
  const activeKey = entries.find(([, profile]) => matchesProfile(config, profile.values))?.[0] ?? null
  // Custom shows the saved values, since no profile describes them
  const shown = activeKey === null ? config : profiles[activeKey].values

  return (
    <>
      <SettingsCard
        title="Spending"
        desc="Pick a starting profile. Vigil stops an investigation that reaches its limits."
      >
        <div className="flex flex-col gap-3.5">
          {profilesPhase === 'loading' && <p className="text-tx-3 text-sm">Loading limits…</p>}
          {profilesPhase === 'error' && <p className="text-sm text-high">Could not read investigation profiles.</p>}
          {profilesPhase === 'ready' && (
            <>
              <div role="group" aria-label="Limits profile" className="su-choices three">
                {entries.map(([key, profile]) => (
                  <ChoiceCard
                    key={key}
                    title={profile.label}
                    body={profileLine(profile.values)}
                    badge={profile.recommended && <span className="su-chip good">Recommended</span>}
                    selected={key === activeKey}
                    onSelect={() => !savingLimits && pickProfile(profile)}
                  />
                ))}
              </div>
              {activeKey === null && (
                <div className="settings-banner info">
                  <Icon name="info" size={14} />
                  <span>Custom limits in effect. Your saved values match no profile.</span>
                </div>
              )}
              <div className="flex flex-col gap-2">
                <button
                  type="button"
                  className="su-toggle"
                  aria-expanded={showLimits}
                  onClick={() => setShowLimits((o) => !o)}
                >
                  <Icon name={showLimits ? 'chevD' : 'chevR'} size={14} />
                  Show the limits
                </button>
                {showLimits && (
                  <dl className="grid gap-0.5">
                    {PROFILE_FIELDS.map((field) => (
                      <div key={field.key} className="flex justify-between gap-3 text-xs">
                        <dt className="text-tx-3">{field.label}</dt>
                        <dd className="text-tx-2">{field.fmt(shown[field.key])}</dd>
                      </div>
                    ))}
                  </dl>
                )}
              </div>
            </>
          )}
          {error && <p className="text-sm text-high">{error}</p>}
          <MonthlyCeiling />
        </div>
      </SettingsCard>

      <SettingsCard title="When a decision needs you" desc="Decisions that can wait never page anyone.">
        <SlackRoute />
      </SettingsCard>

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
    </>
  )
}

/** "3 agents at once, $5.00 per investigation", from the served values; 0 means no cap. */
const profileLine = ({ max_concurrent_agents: agents, max_cost_per_investigation: cost }: InvestigationProfileValues) =>
  `${agents === 0 ? 'Any number of agents' : `${agents} agent${agents === 1 ? '' : 's'}`} at once, ${
    cost === 0 ? 'no cost cap' : `${fmtCost(cost)} per investigation`
  }`
