import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { orchestratorApi } from '../../services/api'

/** The MITRE analyst's agent run; the attack-emulation skill states its goal. */
const EMULATE_WORKFLOW = 'agent-run-mitre_analyst'

/** "Emulate": queues the MITRE analyst with ``goal`` as its brief, opening no Case. The
 * agent picks the host from the listed ones, and its execute call waits in Approvals.
 * Hidden from anyone who can't queue a run (POST /orchestrator/investigations). */
export default function Emulate({ goal, className = 'de-link' }: { goal: string; className?: string }) {
  const { hasPermission } = useAuth()
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<{ text: string; bad?: boolean } | null>(null)
  if (!hasPermission('ai_chat.use') || !hasPermission('automation.read')) return null
  const go = () => {
    setBusy(true)
    orchestratorApi.investigate({ workflow_id: EMULATE_WORKFLOW, hypothesis: `${goal} Read the attack-emulation skill.`, no_case: true })
      .then(() => setSaid({ text: 'Emulation queued' }))
      .catch((e) => setSaid({ text: errorText(e, 'The emulation was not queued.'), bad: true }))
      .finally(() => setBusy(false))
  }
  if (said) {
    return (
      <span className="de-emulate-said" role="status">
        {said.bad ? <span className="de-x-err">{said.text}</span> : <>{said.text} · its execute call waits in <Link className="de-link" to="/home">Needs your attention</Link></>}
      </span>
    )
  }
  return (
    <button type="button" className={className} disabled={busy} title="Run the MITRE analyst to emulate this on a listed host; its execute call waits for an approval" onClick={go}>
      {busy ? 'Queuing…' : 'Emulate'}
    </button>
  )
}
