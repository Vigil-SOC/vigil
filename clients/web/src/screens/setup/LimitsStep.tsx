import { useEffect, useRef, useState } from 'react'
import { getAllIntegrations } from '../../config/integrations'
import { configApi } from '../../services/api'
import { bifrostApi } from '../../services/bifrostApi'
import { fmtCost } from '../../shared/cost'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { NotMeasured } from '../../shared/NotMeasured'
import { ConfirmDialog, Field, SettingsCard, TextInput } from '../../shared/ui'
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
import { bifrostError } from '../settings/useBifrost'
import { fetchCeiling, type CeilingState } from './ceiling'
import ChoiceCard from './ChoiceCard'
import ConnectFields from './ConnectFields'
import { fieldsOf, missingFields, type ConnectConfig } from './connectConfig'
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

/* ---------------- Monthly ceiling ----------------
   Resolution lives in ./ceiling: the default key is found by the quota
   endpoint's key name, never by matching its (masked) value. */

/** 0 is "no ceiling", which is higher than any finite limit. */
const effectiveLimit = (n: number) => (n === 0 ? Number.POSITIVE_INFINITY : n)

/* ---------------- Slack ---------------- */

type SlackState = { kind: 'loading' } | { kind: 'error' } | { kind: 'ready'; connected: boolean }

interface IntegrationsConfig {
  enabled_integrations: string[]
  integrations: Record<string, ConnectConfig>
  secrets_set: Record<string, Record<string, boolean>>
}

export default function LimitsStep() {
  const [profiles, setProfiles] = useState<InvestigationProfiles>({})
  // The one current copy of the stored config; every save writes the whole of it.
  const [config, setConfig] = useState<OrchestratorConfig>(ORCHESTRATOR_DEFAULTS)
  const [pendingLimits, setPendingLimits] = useState<OrchestratorConfig | null>(null)
  const [savingLimits, setSavingLimits] = useState(false)
  const [profilesPhase, setProfilesPhase] = useState<LoadPhase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [limitsOpen, setLimitsOpen] = useState(false)

  const [ceiling, setCeiling] = useState<CeilingState>({ kind: 'loading' })
  const [ceilingDraft, setCeilingDraft] = useState('')
  const [pendingCeiling, setPendingCeiling] = useState<number | null>(null)
  const [savingCeiling, setSavingCeiling] = useState(false)
  const [ceilingError, setCeilingError] = useState<string | null>(null)

  const [slack, setSlack] = useState<SlackState>({ kind: 'loading' })
  const [slackFormOpen, setSlackFormOpen] = useState(false)
  const [slackConfig, setSlackConfig] = useState<ConnectConfig>({})
  const [slackSecrets, setSlackSecrets] = useState<Record<string, boolean>>({})
  const [slackSaving, setSlackSaving] = useState(false)
  const [slackError, setSlackError] = useState<string | null>(null)
  const [slackBlocked, setSlackBlocked] = useState<string | null>(null)
  // loaded once, so the Slack save merges instead of clobbering other integrations
  const integrations = useRef<IntegrationsConfig>({
    enabled_integrations: [],
    integrations: {},
    secrets_set: {},
  })
  const slackIntegration = getAllIntegrations().find((i) => i.id === 'slack')

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

  useEffect(() => {
    let live = true
    fetchCeiling().then((next) => {
      if (!live) return
      setCeiling(next)
      if (next.kind === 'ready') setCeilingDraft(next.currentLimit > 0 ? String(next.currentLimit) : '')
    })
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    let live = true
    configApi
      .getIntegrations()
      .then(({ data }) => {
        if (!live) return
        const d = (data ?? {}) as Partial<IntegrationsConfig>
        integrations.current = {
          enabled_integrations: d.enabled_integrations || [],
          integrations: d.integrations || {},
          secrets_set: d.secrets_set || {},
        }
        setSlackConfig(integrations.current.integrations['slack'] ?? {})
        setSlackSecrets(integrations.current.secrets_set['slack'] ?? {})
        // Slack's own status, not the setup-steps `notify` flag: that one is
        // also set by PagerDuty, so it would call a PagerDuty-only install
        // "Slack: Connected".
        setSlack({
          kind: 'ready',
          connected: integrations.current.secrets_set['slack']?.bot_token === true,
        })
      })
      .catch(() => {
        if (live) setSlack({ kind: 'error' })
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
    if (savingLimits) return
    const next = { ...config, ...profile.values }
    if (raisesLimit(config, next)) setPendingLimits(next)
    else saveLimits(next)
  }

  const saveCeiling = async (value: number) => {
    if (ceiling.kind !== 'ready') return
    const { key } = ceiling
    setSavingCeiling(true)
    setCeilingError(null)
    try {
      // The full body VirtualKeyDialog sends, not budget alone: nothing in the
      // repo shows a partial PUT is safe, and a budget-only body risks Bifrost
      // taking the absent name, rate limit and allow-lists literally. The
      // key's (masked) value is never part of the body.
      await bifrostApi.updateVirtualKey(key.id, {
        name: key.name,
        description: key.description || undefined,
        is_active: key.is_active,
        allowed_models: key.allowed_models,
        allowed_providers: key.allowed_providers,
        budget: value > 0 ? { max_limit: value, reset_duration: ceiling.resetDuration } : null,
        rate_limit: key.rate_limit ?? null,
      })
      // Reload, so the field shows what Bifrost holds now, not what was typed.
      const next = await fetchCeiling()
      setCeiling(next)
      if (next.kind === 'ready') setCeilingDraft(next.currentLimit > 0 ? String(next.currentLimit) : '')
    } catch (err) {
      setCeilingError(bifrostError(err, 'Could not save the monthly ceiling.'))
    } finally {
      setSavingCeiling(false)
    }
  }

  const requestSaveCeiling = () => {
    if (ceiling.kind !== 'ready') return
    const raw = ceilingDraft.trim()
    const value = raw === '' ? 0 : Number(raw)
    if (!Number.isFinite(value) || value < 0) {
      setCeilingError('Enter a ceiling amount, or clear the field for no ceiling.')
      return
    }
    setCeilingError(null)
    if (value === ceiling.currentLimit) return
    if (effectiveLimit(value) > effectiveLimit(ceiling.currentLimit)) setPendingCeiling(value)
    else saveCeiling(value)
  }

  const saveSlack = async () => {
    if (!slackIntegration) return
    const missing = missingFields(slackIntegration, slackConfig, slackSecrets)
    if (missing.length) {
      setSlackBlocked(`Please fill in: ${missing.map((f) => f.label).join(', ')}`)
      return
    }
    setSlackBlocked(null)
    setSlackError(null)
    setSlackSaving(true)
    try {
      const cur = integrations.current
      const nextIntegrations = { ...cur.integrations, slack: slackConfig }
      const enabled = cur.enabled_integrations.includes('slack')
        ? cur.enabled_integrations
        : [...cur.enabled_integrations, 'slack']
      await configApi.setIntegrations({ enabled_integrations: enabled, integrations: nextIntegrations })
      const secrets = { ...cur.secrets_set['slack'] }
      for (const f of fieldsOf(slackIntegration))
        if (f.type === 'password' && slackConfig[f.name]) secrets[f.name] = true
      integrations.current = {
        enabled_integrations: enabled,
        integrations: nextIntegrations,
        secrets_set: { ...cur.secrets_set, slack: secrets },
      }
      setSlackSecrets(secrets)
      setSlack({ kind: 'ready', connected: secrets['bot_token'] === true })
      setSlackFormOpen(false)
    } catch (err) {
      setSlackError(errorText(err, 'Could not save the Slack connection.'))
    } finally {
      setSlackSaving(false)
    }
  }

  const entries = Object.entries(profiles)
  const activeKey = entries.find(([, profile]) => matchesProfile(config, profile.values))?.[0] ?? null
  // Custom shows the saved values, since no profile describes them
  const shown = activeKey === null ? config : profiles[activeKey].values

  return (
    <div className="flex flex-col gap-4">
      <SettingsCard
        title="Spending"
        desc="Pick a starting profile. Vigil stops an investigation that reaches its limits."
      >
        <div className="flex flex-col gap-3.5">
          {profilesPhase === 'loading' && <p className="text-tx-3 text-sm">Loading limits…</p>}
          {profilesPhase === 'error' && (
            <p className="text-sm text-high">Could not read investigation profiles.</p>
          )}
          {profilesPhase === 'ready' && (
            <>
              <div role="group" aria-label="Limits profile" className="su-choices three">
                {entries.map(([key, profile]) => (
                  <ChoiceCard
                    key={key}
                    title={profile.label}
                    body={`${profile.values.max_concurrent_agents} agents at once, ${fmtCost(profile.values.max_cost_per_investigation)} per investigation`}
                    badge={profile.recommended ? <span className="chip">Recommended</span> : undefined}
                    selected={key === activeKey}
                    onSelect={() => pickProfile(profile)}
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
                <button
                  type="button"
                  className="su-toggle"
                  aria-expanded={limitsOpen}
                  onClick={() => setLimitsOpen((o) => !o)}
                >
                  <Icon name={limitsOpen ? 'chevD' : 'chevR'} size={14} />
                  Show the limits
                </button>
                {limitsOpen && (
                  <dl className="grid gap-0.5 mt-1">
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

          <div className="su-collect flex flex-col gap-3">
            {ceiling.kind === 'loading' && <p className="su-note">Loading monthly ceiling…</p>}
            {ceiling.kind === 'budget-error' && (
              <p className="text-sm text-high">Could not read the budget settings.</p>
            )}
            {ceiling.kind === 'resolve-error' && (
              <p className="text-sm text-high">Could not read the default virtual key from the gateway.</p>
            )}
            {ceiling.kind === 'later' && (
              <Field label="Monthly ceiling">
                <span className="flex items-center gap-1.5 text-sm text-tx-2">
                  Later
                  <InfoTip
                    label="Monthly ceiling"
                    text="No default virtual key could be found. Set one in Settings › AI models, then set its ceiling here."
                    align="start"
                  />
                </span>
              </Field>
            )}
            {ceiling.kind === 'ready' && (
              <>
                <div className="su-ceiling">
                  <Field label="Monthly ceiling" hint="The model gateway checks it before every call.">
                    <span className="su-ceiling-field">
                      <TextInput
                        aria-label="Monthly ceiling"
                        inputMode="decimal"
                        placeholder="No ceiling"
                        value={ceilingDraft}
                        onChange={(e) => setCeilingDraft(e.target.value)}
                      />
                      {ceiling.period !== 'monthly' && (
                        <span className="su-note">Resets {ceiling.period}</span>
                      )}
                    </span>
                  </Field>
                  <div className="su-estimate">
                    <NotMeasured label="Monthly estimate" tip="Nothing projects monthly spend yet." />
                  </div>
                </div>
                <div className="su-actions">
                  <button
                    type="button"
                    className="btn primary"
                    disabled={savingCeiling}
                    onClick={requestSaveCeiling}
                  >
                    {savingCeiling ? 'Saving…' : 'Save ceiling'}
                  </button>
                  {ceilingError && (
                    <span className="su-note err" role="alert">
                      {ceilingError}
                    </span>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      </SettingsCard>

      <SettingsCard title="When a decision needs you" desc="Decisions that can wait never page anyone.">
        <div className="flex flex-col gap-3.5">
          <div className="su-notify-row">
            <span className="su-choice-icon">
              <Icon name="send" size={16} />
            </span>
            <span className="su-notify-text">
              <span className="su-notify-name">Slack</span>
              <span className="su-note">Post urgent notifications to a channel</span>
            </span>
            {slack.kind === 'ready' &&
              (slack.connected ? (
                <span className="chip">Connected</span>
              ) : (
                !slackFormOpen &&
                slackIntegration && (
                  <button type="button" className="btn ghost" onClick={() => setSlackFormOpen(true)}>
                    Connect Slack
                  </button>
                )
              ))}
          </div>
          {slack.kind === 'loading' && <p className="su-note">Loading Slack status…</p>}
          {slack.kind === 'error' && (
            <p className="text-sm text-high">Could not read integration status.</p>
          )}
          {slackFormOpen && slackIntegration && (
            <div className="flex flex-col gap-3.5">
              <ConnectFields
                integration={slackIntegration}
                config={slackConfig}
                secretsSet={slackSecrets}
                onChange={(name, value) => setSlackConfig((c) => ({ ...c, [name]: value }))}
              />
              <div className="su-actions">
                <button
                  type="button"
                  className="btn primary"
                  disabled={slackSaving}
                  onClick={saveSlack}
                >
                  {slackSaving ? 'Saving…' : 'Save'}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  disabled={slackSaving}
                  onClick={() => setSlackFormOpen(false)}
                >
                  Cancel
                </button>
                {slackBlocked ? (
                  <span className="su-note err" role="alert">
                    {slackBlocked}
                  </span>
                ) : (
                  <span className="su-note">Secrets are stored encrypted and never shown again.</span>
                )}
              </div>
              {slackError && <p className="text-sm text-high">{slackError}</p>}
            </div>
          )}
        </div>
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

      <ConfirmDialog
        open={pendingCeiling !== null}
        title="Raise the monthly ceiling?"
        body="This increases how much the default virtual key can spend before the gateway stops it. Confirm to save."
        confirmLabel="Save"
        danger={false}
        onConfirm={() => {
          const next = pendingCeiling
          setPendingCeiling(null)
          if (next !== null) saveCeiling(next)
        }}
        onClose={() => setPendingCeiling(null)}
      />
    </div>
  )
}
