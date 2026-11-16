import { useCallback, useEffect, useState, type FormEvent, type KeyboardEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { utcDayClock } from '../../shared/utc'
import {
  detectionEngineeringApi,
  detectionRulesApi,
  type Proposal,
  type ProposalGap,
  type ProposalRow,
  type RuleCheck,
  type RuleDetail,
} from '../../services/api'
import { Empty, Failure } from '../shared/x1'
import { who, type Stage } from './ruleStage'

/** The origin the daily gaps run puts on the drafts it raises. */
const GAPS_RUN = 'gaps run'
const LANG: Record<string, string> = { splunk: 'Splunk', elastic: 'Elastic', wazuh: 'Wazuh', sigma: 'Sigma' }
const STAGE_WORD: Record<Stage, string> = { shadow: 'Shadow', live: 'Live', off: 'Off' }
const KIND: Record<ProposalGap['kind'], { word: string; tone: string }> = {
  missed: { word: 'Missed', tone: 'poor' },
  paper: { word: 'Paper only', tone: 'fair' },
  intel: { word: 'Intel', tone: '' },
  blind: { word: 'Blind spot', tone: 'fair' },
}

const plural = (v: number, one: string, many: string) => `${v.toLocaleString()} ${v === 1 ? one : many}`
const short = (id: string) => id.slice(0, 8)
const lang = (l: string) => LANG[l] ?? l

/** A rule copy as the rule listing sends it: only the fields the list reads. */
interface Listed {
  rule_id: number
  name: string
  language: string
  stage?: Stage | null
  days_in_shadow?: number | null
}

/** The copy a proposal's state follows: a SIEM copy before its Sigma form. */
function mainRule(ids: number[], listed: Map<number, Listed>): Listed | null {
  const rows = ids.map((id) => listed.get(id)).filter((r): r is Listed => !!r)
  return rows.find((r) => r.language !== 'sigma') ?? rows[0] ?? null
}

/** Where a proposal stands, in the rule list's own health pill. */
function standing(p: ProposalRow, listed: Map<number, Listed> | null): { label: string; tone: string; gone?: boolean } {
  if (!p.rule_ids.length) return p.run_id ? { label: 'No rule yet', tone: 'none' } : { label: 'Waiting for a run', tone: 'none' }
  if (!listed) return { label: plural(p.rule_ids.length, 'rule', 'rules'), tone: 'none' }
  const rule = mainRule(p.rule_ids, listed)
  if (!rule) return { label: 'Removed', tone: 'idle', gone: true }
  if (rule.stage === 'shadow') return { label: `Shadow · ${plural(rule.days_in_shadow ?? 0, 'day', 'days')}`, tone: 'fair' }
  if (rule.stage === 'live') return { label: 'Live', tone: 'good' }
  if (rule.stage === 'off') return { label: 'Switched off', tone: 'idle', gone: true }
  return { label: 'Saved', tone: 'none' }
}

/** The gap in words, from what the run recorded. */
function gapLine(g: ProposalGap): string {
  if (g.kind === 'missed') return `${plural(g.count, 'run', 'runs')} no rule caught${g.confirmed ? `, ${g.confirmed} confirmed` : ''}`
  if (g.kind === 'paper') return `${plural(g.count, 'alert', 'alerts')} on a technique only rules on paper cover`
  if (g.kind === 'intel') return `Named in recent threat intel${g.sources.length ? ` by ${g.sources.join(', ')}` : ''}, with no proven rule`
  return `${plural(g.count, 'rule', 'rules')} starved of data`
}

/** Rules Vigil drafted after a proven gap: the list, and beside it the picked one with why
 * it was drafted, the rule, what it was checked against and what a person can decide. */
export default function ProposedRules() {
  const [params, setParams] = useSearchParams()
  const [rows, setRows] = useState<ProposalRow[] | null>(null)
  const [listed, setListed] = useState<Map<number, Listed> | null>(null)
  const [error, setError] = useState<string | null>(null)

  const readRules = useCallback((props: ProposalRow[]) => {
    const ids = [...new Set(props.flatMap((p) => p.rule_ids))]
    if (!ids.length) { setListed(new Map()); return }
    detectionRulesApi.rules({ rules: ids.join(',') })
      .then((res) => setListed(new Map((res.data as { rules: Listed[] }).rules.map((r) => [r.rule_id, r]))))
      .catch(() => setListed(null))
  }, [])

  useEffect(() => {
    let live = true
    detectionEngineeringApi.proposals()
      .then((res) => {
        if (!live) return
        // Vigil's own drafts after a gap; one a person or Vigil rejected is off the list.
        const mine = res.data.proposals.filter((p) => p.origin?.source === GAPS_RUN && p.state !== 'rejected')
        setRows(mine)
        setError(null)
        readRules(mine)
      })
      .catch((e) => { if (live) setError(errorText(e, 'Proposed rules did not load.')) })
    return () => { live = false }
  }, [readRules])

  if (error) return <Failure>{error}</Failure>
  if (!rows) return <p className="x1-muted de-note">Reading proposed rules…</p>
  if (!rows.length) {
    return (
      <Empty title="Vigil has not proposed a rule yet">
        The daily gaps run drafts one here when a proven attack, alerts on a paper-only technique or recent threat intel show a technique no rule catches.
      </Empty>
    )
  }

  const pickedId = params.get('proposal')
  const picked = rows.find((p) => p.proposal_id === pickedId) ?? rows[0]
  const pick = (id: string) => setParams({ tab: 'proposed', proposal: id }, { replace: true })
  const onKey = (e: KeyboardEvent<HTMLLIElement>, id: string) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(id) }
  }

  return (
    <div className="de-prop">
      <section className="x1-card de-card de-prop-list">
        <div className="de-card-head de-prop-list-head">
          <div>
            <h2>Proposed rules <span>· {rows.length.toLocaleString()}</span></h2>
            <p>Vigil’s drafts after a proven miss</p>
          </div>
        </div>
        <ul className="list-scroll de-prop-rows" role="listbox" aria-label="Proposed rules">
          {rows.map((p) => {
            const st = standing(p, listed)
            const rule = listed ? mainRule(p.rule_ids, listed) : null
            const sel = p.proposal_id === picked.proposal_id
            return (
              <li
                key={p.proposal_id}
                role="option"
                aria-selected={sel}
                tabIndex={0}
                className={`case-row${sel ? ' sel' : ''}${st.gone ? ' de-prop-gone' : ''}`}
                onClick={() => pick(p.proposal_id)}
                onKeyDown={(e) => onKey(e, p.proposal_id)}
              >
                <div className="cr-top"><b className="de-prop-name">{rule?.name ?? p.title}</b></div>
                <div className="cr-meta">
                  <span className={`de-health ${st.tone}`}><i />{st.label}</span>
                  {p.technique && <span className="mono de-prop-tech">{p.technique}</span>}
                </div>
                <div className="cr-meta de-prop-sub">
                  {p.run_id ? `Gaps run · run ${short(p.run_id)}` : 'Gaps run'} · {utcDayClock(p.created_at)}
                </div>
              </li>
            )
          })}
        </ul>
      </section>
      <ProposalDetail key={picked.proposal_id} id={picked.proposal_id} onChanged={() => readRules(rows)} />
    </div>
  )
}

/** The picked proposal and the rule copies attached to it; a copy no longer listed is null. */
function ProposalDetail({ id, onChanged }: { id: string; onChanged: () => void }) {
  const canWrite = useAuth().hasPermission('detections.write')
  const [proposal, setProposal] = useState<Proposal | null>(null)
  const [rules, setRules] = useState<(RuleDetail | null)[]>([])
  const [error, setError] = useState<string | null>(null)
  const [copy, setCopy] = useState<string | null>(null)
  const [asking, setAsking] = useState(false)

  const load = useCallback(() => {
    let live = true
    detectionEngineeringApi.proposal(id)
      .then(async (res) => {
        const reads = await Promise.allSettled(res.data.rule_ids.map((r) => detectionRulesApi.rule(r)))
        if (!live) return
        setProposal(res.data)
        setRules(reads.map((r) => (r.status === 'fulfilled' ? r.value.data : null)))
        setError(null)
      })
      .catch((e) => { if (live) setError(errorText(e, 'This proposal did not load.')) })
    return () => { live = false }
  }, [id])
  useEffect(load, [load])

  if (error) return <div className="de-prop-detail"><Failure>{error}</Failure></div>
  if (!proposal) return <div className="de-prop-detail"><p className="x1-muted de-note">Reading the proposal…</p></div>

  const shown = rules.filter((r): r is RuleDetail => !!r)
  const main = shown.find((r) => r.source !== 'sigma') ?? shown[0] ?? null
  const gap = proposal.origin.gap
  const attach = [...proposal.events].reverse().find((e) => e.evidence?.step === 'attach')
  // The rule's text by copy: a listed copy's latest query, else what the run saved.
  const texts: [string, string][] = shown.length
    ? shown.map((r) => [r.source, r.source === 'sigma' && proposal.sigma ? proposal.sigma : r.query])
    : [...Object.entries(proposal.copies), ...(proposal.sigma ? [['sigma', proposal.sigma] as [string, string]] : [])]
  const langs = texts.map(([l]) => lang(l)).join(' + ')
  const on = texts.find(([l]) => l === copy) ?? texts[0]
  const switchable = canWrite && main?.stage === 'shadow'
  // The main copy's moves: from its rule read, else (a removed copy) from the proposal's record.
  const moves = main?.stages ?? proposal.stages.filter((m) => m.rule_id === (proposal.rule_id ?? proposal.rule_ids[0]))
  const changed = () => { setAsking(false); load(); onChanged() }

  return (
    <div className="de-prop-detail">
      <section className="x1-card de-card">
        <div className="de-card-head">
          <div>
            <h2>{main?.title ?? proposal.title}</h2>
            <p>
              {[
                proposal.technique,
                langs || null,
                attach ? `drafted by ${who(attach.actor)}, ${utcDayClock(attach.at)}` : `raised by the gaps run, ${utcDayClock(proposal.created_at)}`,
              ].filter(Boolean).join(' · ')}
            </p>
          </div>
          {switchable && !asking && (
            <button type="button" className="btn" onClick={() => setAsking(true)}>Switch off</button>
          )}
        </div>
        {switchable && asking && main && <SwitchOff ruleId={main.rule_id} onDone={(done) => (done ? changed() : setAsking(false))} />}
      </section>

      <section className="x1-card de-card">
        <div className="de-card-head">
          <div>
            <h2>Why Vigil proposed it</h2>
            <p>From the gap to the rule</p>
          </div>
        </div>
        <WhyChain proposal={proposal} gap={gap} main={main} stage={moves.length ? moves[moves.length - 1].stage : null} langs={langs} attachAt={attach?.at ?? null} attachBy={attach?.actor ?? null} />
      </section>

      <section className="x1-card de-card">
        <div className="de-card-head">
          <div>
            <h2>The rule</h2>
            <p>{main?.description || (texts.length ? 'What the run saved for this gap' : 'The run has not saved a rule for this gap')}</p>
          </div>
          {texts.length > 1 && (
            <div className="de-prop-copies" role="group" aria-label="Rule copy">
              {texts.map(([l]) => (
                <button key={l} type="button" aria-pressed={on?.[0] === l} className={`btn ${on?.[0] === l ? 'primary' : 'ghost'}`} onClick={() => setCopy(l)}>{lang(l)}</button>
              ))}
            </div>
          )}
        </div>
        {on && <pre className="de-raw de-prop-query">{on[1]}</pre>}
      </section>

      <section className="x1-card de-card">
        <div className="de-card-head">
          <div>
            <h2>Checked against</h2>
            <p>What Vigil ran before it saved the rule, and since</p>
          </div>
        </div>
        <Checks proposal={proposal} rules={rules} shown={shown} main={main} moves={moves} />
      </section>
    </div>
  )
}

function WhyChain({ proposal, gap, main, stage, langs, attachAt, attachBy }: {
  proposal: Proposal
  gap: ProposalGap | undefined
  main: RuleDetail | null
  /** The main copy's latest stage, read or recorded. */
  stage: Stage | null
  langs: string
  attachAt: string | null
  attachBy: string | null
}) {
  const kind = gap ? KIND[gap.kind] : null
  const runs = gap?.runs ?? []
  const alerts = gap?.alerts ?? []
  const sources = gap?.sources ?? []
  const evidence = runs.length + alerts.length + sources.length
  const withheld = (gap?.runs_withheld ?? 0) > 0 || !!gap?.alerts_withheld
  return (
    <ul className="de-rollout-cells">
      <li>
        <span className="de-rollout-name">1 · Gap</span>
        <b className={kind?.tone}>{kind?.word ?? '—'}</b>
        <span className="de-rollout-line">{gap ? `${gap.technique}: ${gapLine(gap)}` : proposal.title}</span>
      </li>
      <li>
        <span className="de-rollout-name">2 · Evidence</span>
        <b>{evidence.toLocaleString()}</b>
        <span className="de-rollout-line">
          {runs.map((k, i) => {
            const [what, ref] = [k.slice(0, k.indexOf(':')), k.slice(k.indexOf(':') + 1)]
            return (
              <span key={k}>
                {i > 0 && ', '}
                {what === 'case'
                  ? <Link className="de-link" to={`/cases?case=${encodeURIComponent(ref)}`}>Case {ref}</Link>
                  : <>{what === 'hunt' ? 'Hunt' : what} <span className="mono">{ref}</span></>}
              </span>
            )
          })}
          {alerts.length > 0 && <>{runs.length > 0 && ' · '}{plural(alerts.length, 'alert', 'alerts')}: <span className="mono">{alerts.join(', ')}</span></>}
          {sources.length > 0 && <>{runs.length + alerts.length > 0 && ' · '}Named by {sources.join(', ')}</>}
          {!evidence && !withheld && 'None recorded'}
        </span>
        {withheld && (
          <span className="de-rollout-target">
            {[
              gap?.runs_withheld ? `${plural(gap.runs_withheld, 'Case', 'Cases')} you can’t read` : null,
              gap?.alerts_withheld ? 'Its alerts: you can’t read findings' : null,
            ].filter(Boolean).join(' · ')}
          </span>
        )}
      </li>
      <li>
        <span className="de-rollout-name">3 · Run</span>
        <b>{proposal.run_id ? short(proposal.run_id) : '—'}</b>
        <span className="de-rollout-line">{proposal.run_id ? 'Vigil handed the gap to an agent run to draft a rule' : 'Not handed to a run yet'}</span>
        {proposal.run_id && (
          <Link className="de-rollout-target de-link" to={`/workflows?run=${encodeURIComponent(proposal.run_id)}`}>Open run</Link>
        )}
      </li>
      <li>
        <span className="de-rollout-name">4 · Rule</span>
        <b className={stage === 'shadow' && main ? 'fair' : stage === 'live' && main ? 'good' : undefined}>
          {stage ? STAGE_WORD[stage] : proposal.rule_ids.length ? (main ? 'Saved' : 'Removed') : '—'}
        </b>
        <span className="de-rollout-line">
          {proposal.rule_ids.length
            ? `Saved${attachAt ? ` ${utcDayClock(attachAt)}` : ''}${attachBy ? ` by ${who(attachBy)}` : ''}${langs ? ` as ${langs}` : ''}${main ? '' : '; no longer listed'}`
            : proposal.run_id ? 'The run has not saved a rule for this gap' : 'No rule until a run drafts one'}
        </span>
      </li>
    </ul>
  )
}

const passWord = (c: RuleCheck | undefined, pass: string, fail: string) =>
  !c?.result ? null : c.result === 'pass' ? pass : c.result === 'fail' ? fail : 'Not known'

/** A replay's result in words: the proven events it caught, and the ruled-out ones. */
function replayWord(c: RuleCheck): string {
  if (c.result !== 'pass') return c.result === 'fail' ? 'Missed' : 'Not known'
  const proven = Number(c.proven_events ?? 0)
  const out = Number(c.ruled_out_events ?? 0)
  return `Caught ${plural(proven, 'event', 'events')}${out ? `, ${c.ruled_out_matched ? 'matched' : 'cleared'} ${out} ruled out` : ''}`
}

function Checks({ proposal, rules, shown, main, moves }: {
  proposal: Proposal
  rules: (RuleDetail | null)[]
  shown: RuleDetail[]
  main: RuleDetail | null
  moves: { stage: Stage; actor: string; reason: string; at: string }[]
}) {
  if (!proposal.rule_ids.length) return <p className="de-card-sub">Nothing yet: there is no rule to check.</p>
  const before: { what: string; said: string; title?: string }[] = []
  for (const r of shown) {
    const lint = passWord(r.checks.lint, 'Passed', 'Failed')
    if (lint) before.push({ what: `Lint · ${lang(r.source)}`, said: lint, title: r.checks.lint?.reason ?? undefined })
    const replay = r.checks.replay
    if (replay?.result) before.push({ what: `Replay · ${lang(r.source)}`, said: replayWord(replay), title: replay.reason ?? undefined })
  }
  const runs = (main?.checks.runs?.days ?? {}) as Record<string, { pass?: number; fail?: number }>
  const hits = (main?.checks.shadow?.days ?? {}) as Record<string, number>
  const runDays = Object.values(runs)
  const passedDays = runDays.filter((d) => (d.pass ?? 0) > 0 && !(d.fail ?? 0)).length
  const hitDays = Object.values(hits)
  const sees = main?.checks.sees
  const since: { what: string; said: string; title?: string }[] = []
  if (runDays.length) since.push({ what: 'Daily runs', said: `${passedDays} of ${runDays.length} passed` })
  if (hitDays.length) since.push({ what: 'Hits per day', said: `at most ${Math.max(...hitDays).toLocaleString()}` })
  if (sees?.result) since.push({ what: `Sees · ${String(sees.data_source ?? 'its data')}`, said: passWord(sees, 'Data arriving', 'No data') ?? '', title: sees.reason ?? undefined })
  const last = moves[moves.length - 1]
  const gone = rules.length - shown.length
  return (
    <>
      {before.length > 0 && <Group title="Before saving" rows={before} />}
      {since.length > 0 && <Group title={main?.stage === 'shadow' ? 'In shadow' : 'Since saving'} rows={since} />}
      {moves.length > 0 && (
        <Group
          title="Rollout"
          rows={[...moves].reverse().map((m) => ({ what: `${STAGE_WORD[m.stage]} · ${utcDayClock(m.at)}`, said: who(m.actor), title: m.reason || undefined }))}
        />
      )}
      {last?.reason && <p className="de-card-sub">Last move, by {who(last.actor)}: {last.reason}</p>}
      {gone > 0 && <p className="de-card-sub">{plural(gone, 'copy is', 'copies are')} no longer listed (removed), so {gone === 1 ? 'its' : 'their'} checks can’t be read.</p>}
    </>
  )
}

function Group({ title, rows }: { title: string; rows: { what: string; said: string; title?: string }[] }) {
  return (
    <>
      <h3 className="de-group">{title}</h3>
      <ul className="de-spots">
        {rows.map((r) => (
          <li key={r.what} title={r.title}>
            <code>{r.what}</code>
            <span>{r.said}</span>
          </li>
        ))}
      </ul>
    </>
  )
}

/** Switch off: the reason is required, and the rule then waits for a person. */
function SwitchOff({ ruleId, onDone }: { ruleId: number; onDone: (changed: boolean) => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!reason.trim()) { setError('Say why.'); return }
    setBusy(true)
    detectionRulesApi.switchOffRule(ruleId, reason.trim())
      .then(() => onDone(true))
      .catch((err) => { setError(errorText(err, 'Could not switch it off.')); setBusy(false) })
  }
  return (
    <form className="de-x-form de-prop-ask" onSubmit={submit}>
      <span className="de-card-sub">Vigil stops running it, and a later version waits in shadow for a person.</span>
      <input className="de-x-in" aria-label="Reason" placeholder="Why switch it off (required)" value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      {error && <span className="de-x-err" role="alert">{error}</span>}
      <div className="de-x-actions">
        <button type="submit" className="btn de-x-btn danger" disabled={busy}>{busy ? 'Switching off…' : 'Switch off'}</button>
        <button type="button" className="btn ghost de-x-btn" onClick={() => onDone(false)}>Cancel</button>
      </div>
    </form>
  )
}
