import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { detectionEngineeringApi, type LinkedProposal } from '../../services/api'
import { errorText } from '../../shared/errorText'
import HuntRef from './HuntRef'
import { proposalHref, proposalWord } from './proposalLinks'

/** The hunts behind a proposal, each linked to its run. */
function ProposalHunts({ p }: { p: LinkedProposal }) {
  const ids = Object.keys(p.hunts)
  if (!ids.length) return null
  return (
    <>
      {ids.map((h, i) => (
        <span key={h}>{i > 0 && ', '}<HuntRef id={h} run={p.hunts[h]} /></span>
      ))}
    </>
  )
}

/** The proposals a rule copy answers and the hunts behind them, on its History panel; nothing
 * when no gaps-run proposal names it. */
export default function RuleProposals({ ruleId }: { ruleId: number }) {
  const [rows, setRows] = useState<LinkedProposal[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    detectionEngineeringApi.ruleProposals(ruleId)
      .then((res) => { if (live) { setRows(res.data.proposals); setError(null) } })
      .catch((e) => { if (live) setError(errorText(e, 'Its proposals did not load.')) })
    return () => { live = false }
  }, [ruleId])
  if (error) return <span className="de-x-err" role="alert">{error}</span>
  if (!rows?.length) return null
  return (
    <>
      <div className="de-x-head"><b>Proposed by Vigil <span>· {rows.length.toLocaleString()}</span></b></div>
      <ul className="de-x-list">
        {rows.map((p) => (
          <li key={p.proposal_id}>
            <span className="de-st-why">
              <Link className="de-link" to={proposalHref(p.proposal_id)}>{p.title}</Link>
              {Object.keys(p.hunts).length > 0 && <> · from <ProposalHunts p={p} /></>}
            </span>
            <span className="x1-muted">{[p.technique, proposalWord(p)].filter(Boolean).join(' · ')}</span>
          </li>
        ))}
      </ul>
    </>
  )
}
