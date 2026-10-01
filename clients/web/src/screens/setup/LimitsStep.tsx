import { useEffect, useState } from 'react'
import { configApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import { ConfirmDialog } from '../../shared/ui'
import type {
  InvestigationProfile,
  InvestigationProfiles,
  InvestigationProfileValues,
} from '../settings/useSettings'

const PROFILE_FIELDS: { key: keyof InvestigationProfileValues; label: string }[] = [
  { key: 'max_cost_per_investigation', label: 'Max cost per investigation' },
  { key: 'max_iterations_per_agent', label: 'Max iterations per agent' },
  { key: 'max_runtime_per_investigation', label: 'Max runtime per investigation' },
  { key: 'max_concurrent_agents', label: 'Max concurrent agents' },
  { key: 'max_total_hourly_cost', label: 'Max hourly cost' },
]

type LoadPhase = 'loading' | 'ready' | 'error'

interface Approval {
  enabled: boolean
  environment_wins: boolean
}

function errorText(err: unknown, fallback: string): string {
  if (typeof err === 'object' && err && 'response' in err) {
    const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
  }
  return fallback
}

export default function LimitsStep() {
  const [profiles, setProfiles] = useState<InvestigationProfiles>({})
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
        const row = (data ?? {}) as { profiles?: InvestigationProfiles }
        setProfiles(row.profiles ?? {})
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

  return (
    <div className="flex flex-col gap-4">
      {profilesPhase === 'error' ? (
        <p className="text-sm text-high">Could not read investigation profiles.</p>
      ) : (
        <div className="flex flex-col gap-2">
          {Object.entries(profiles).map(([key, profile]) => (
            <ProfileCard key={key} profile={profile} />
          ))}
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
          <div className="settings-grid-2" style={{ gridTemplateColumns: 'repeat(2, 1fr)' }}>
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
              <div className="text-[13px] font-semibold text-tx">Act</div>
              <span className="text-xs text-tx-3">Let autonomous response proceed without forcing approval.</span>
            </button>
          </div>
        </>
      )}

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

function ProfileCard({ profile }: { profile: InvestigationProfile }) {
  return (
    <div
      className="text-sm"
      style={{ border: '1px solid var(--line)', borderRadius: 6, padding: '10px 12px' }}
    >
      <div className="text-tx font-medium">{profile.label}</div>
      <dl className="mt-1.5 grid gap-0.5">
        {PROFILE_FIELDS.map((field) => (
          <div key={field.key} className="flex justify-between gap-3 text-xs">
            <dt className="text-tx-3">{field.label}</dt>
            <dd className="text-tx-2">{profile.values[field.key]}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
