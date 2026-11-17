import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { HuntDetections as Read, NoProposalReason } from '../../services/api'
import { copyStage, proposalHref, proposalWord, ruleHref } from '../detections/proposalLinks'

/** Each reason in a word; the row's detail says the rest. */
const REASON: Record<NoProposalReason, string> = {
  outside_window: 'Outside the window',
  no_technique: 'No technique',
  unknown: 'Unknown',
  caught: 'Caught by a rule',
  workflow_off: 'Gaps run turned off',
  held: 'Held',
  waiting: 'Waiting for the next daily run',
  below_floor: 'Below the floor',
}

/** What a concluded hunt led to for detection: the proposals the daily gaps run raised from
 *  it with their rule copies and stage, and why each proven hypothesis without one got none. */
export function HuntDetections({ read, hyp }: { read: Read; hyp: (id: string) => ReactNode }) {
  return (
    <div style={{ marginTop: 12 }}>
      {read.proposals.length > 0 && (
        <>
          <h4>Proposed rules ({read.proposals.length})</h4>
          <div className="muted text-[11.5px] mb-2">Raised from this hunt by the daily gaps run. A rejected one stays here with its rules: a reject does not stop their rollout.</div>
          <div className="table-wrap">
            <table className="tbl">
              <thead><tr><th>Proposal</th><th className="tight">Technique</th><th className="tight">State</th><th>Rules</th></tr></thead>
              <tbody>
                {read.proposals.map((p) => (
                  <tr key={p.proposal_id}>
                    <td><Link to={proposalHref(p.proposal_id)}>{p.title}</Link></td>
                    <td className="tight mono">{p.technique ?? '—'}</td>
                    <td className="tight">{proposalWord(p)}</td>
                    <td>
                      {p.rules.length === 0 && <span className="muted">{p.run_working ? 'Its run is still drafting one' : 'No rule saved'}</span>}
                      {p.rules.map((r) => (
                        <div key={r.rule_id}>
                          {r.status === 'removed'
                            ? <span className="muted">Rule {r.rule_id}</span>
                            : <Link to={ruleHref(r.rule_id)}>{r.name ?? `Rule ${r.rule_id}`}</Link>}
                          <span className="muted text-[11px]"> · {[r.language, r.version != null ? `v${r.version}` : null, copyStage(r)].filter(Boolean).join(' · ')}</span>
                        </div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {read.without.length > 0 && (
        <div style={{ marginTop: read.proposals.length ? 16 : 0 }}>
          <h4>Proven without a proposal ({read.without.length})</h4>
          <div className="muted text-[11.5px] mb-2">Why the daily gaps run raised no rule for each, worked out now.</div>
          <div className="table-wrap">
            <table className="tbl">
              <thead><tr><th className="tight" /><th>Statement</th><th className="tight">Techniques</th><th>Why none</th></tr></thead>
              <tbody>
                {read.without.map((w) => (
                  <tr key={`${w.hunt_id}|${w.hypothesis_id}`}>
                    <td className="tight">{hyp(w.hypothesis_id)}</td>
                    <td>{w.statement}</td>
                    <td className="tight mono">{w.techniques.join(', ') || '—'}</td>
                    <td>
                      {REASON[w.reason]}
                      <div className="muted text-[11px]">{w.detail}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
