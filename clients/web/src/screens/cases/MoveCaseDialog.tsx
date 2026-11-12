import { useState } from 'react'
import { casesApi } from '../../services/api'
import { PersonPicker, onTeam, usePeople } from '../../shared/people'
import { Popup, Select } from '../../shared/ui'
import { errorDetail } from './caseFormat'
import { inputCls } from './CaseSections'

export interface MoveTeam {
  id: string
  name: string
}

/**
 * Move a case onto another team. The owner list is that team's members.
 * One destination is already chosen; several ask which.
 */
export function MoveCaseDialog({
  caseId,
  source,
  initialOwner,
  teams,
  onClose,
  onMoved,
}: {
  caseId: string
  source: MoveTeam
  initialOwner: string
  teams: MoveTeam[]
  onClose: () => void
  onMoved: () => void
}) {
  const only = teams.length === 1 ? teams[0] : null
  const { people } = usePeople()
  const [destination, setDestination] = useState(only?.id ?? '')
  const [owner, setOwner] = useState(only ? initialOwner : '')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const chosen = teams.find((team) => team.id === destination)
  const pickTeam = (id: string) => {
    setDestination(id)
    const person = people.find((one) => one.username === initialOwner)
    setOwner(person && onTeam(person, id) ? person.username : '')
  }

  const submit = async () => {
    if (!chosen || !reason.trim() || busy) return
    setBusy(true)
    setErr('')
    try {
      await casesApi.move(caseId, {
        source_team_id: source.id,
        destination_team_id: chosen.id,
        reason: reason.trim(),
        ...(owner.trim() ? { owner: owner.trim() } : {}),
      })
      onMoved()
    } catch (e) {
      setErr(errorDetail(e, 'Couldn’t move the case'))
      setBusy(false)
    }
  }

  return (
    <Popup open onClose={onClose} title="Move case" width={520}>
      <div className="flex flex-col gap-3.5">
        <p className="text-[13px] text-tx-2 leading-[1.5] m-0">
          Move <span className="mono text-tx">{caseId}</span> from {source.name}.
          {only ? ` It goes to ${only.name}.` : ' Choose the team it goes to.'}
        </p>
        {teams.length === 0 && (
          <p className="text-[13px] m-0">There is no other team to move this case to.</p>
        )}
        {teams.length > 1 && (
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Team</span>
            <Select
              label="Destination team"
              value={destination}
              placeholder="Choose a team"
              options={teams.map((team) => ({ value: team.id, label: team.name }))}
              onSelect={pickTeam}
              searchable={teams.length > 8}
            />
          </label>
        )}
        {chosen && (
          <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
            <span>Owner</span>
            <PersonPicker label="Owner" value={owner} onSelect={setOwner} teamId={chosen.id} />
          </label>
        )}
        {chosen && (
          <p className="text-[12px] text-tx-2 leading-[1.45] m-0">
            The owner is someone on {chosen.name}. Unassigned leaves the case with no owner when the current one is not on that team.
          </p>
        )}
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Reason</span>
          <textarea className={inputCls} rows={3} value={reason} onChange={(e) => setReason(e.target.value)} style={{ resize: 'vertical' }} />
        </label>
        {err && <div className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
        <div className="flex justify-end gap-2.5">
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
          <button type="button" className="btn primary" onClick={() => void submit()} disabled={busy || !chosen || !reason.trim()}>
            {busy ? 'Moving…' : 'Move case'}
          </button>
        </div>
      </div>
    </Popup>
  )
}
