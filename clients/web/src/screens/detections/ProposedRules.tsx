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
import { unprovenNote, who, type Stage, type StageFields } from './ruleStage'
import { caseLabel } from '../../shared/caseLabel'
import HuntRef from './HuntRef'
import Emulate from './Emulate'

/** The origin the daily gaps run puts on the drafts it raises. */
const GAPS_RUN = 'gaps run'
/** The origin from_rule puts on a proposal to prove an inventory rule (Propose on the Rules tab). */
const INVENTORY = 'inventory'
const fromInventory = (p: ProposalRow) => p.origin?.source === INVENTORY
const LANG: Record<string, string> = { splunk: 'Splunk', elastic: 'Elastic', wazuh: 'Wazuh', sigma: 'Sigma' }
const STAGE_WORD: Record<Stage, string> = { shadow: 'Shadow', live: 'Live', off: 'Off' }
const KIND: Record<ProposalGap['kind'], { word: string; tone: string }> = {
  missed: { word: 'Missed', tone: 'poor' },
  paper: { word: 'Paper only', tone: 'fair' },
  intel: { word: 'Intel', tone: '' },
  blind: { word: 'Blind spot', tone: 'fair' },
}

/** Rule ids per rule listing read: under its 300-row cap and its 4000-character ``rules``. */
const RULES_PER_READ = 200
/** A run that ended, in the words a proposal it saved no rule for reads with. */
const RUN_ENDED: Record<string, string> = { completed: 'ended', failed: 'failed', cancelled: 'was cancelled' }

const plural = (v: number, one: string, many: string) => `${v.toLocaleString()} ${v === 1 ? one : many}`
const short = (id: string) => id.slice(0, 8)
const lang = (l: string) => LANG[l] ?? l

/** A rule copy as the rule listing sends it: only the fields this tab reads. */
interface Listed extends StageFields {
  rule_id: number
  name: string
  language: string
}

/** The ids a proposal's state is read from: the copy its stages follow, else every copy. */
const followed = (p: ProposalRow) => (p.rule_id != null ? [p.rule_id] : p.rule_ids)

/** The copy a proposal's state follows: the one its stages follow (the latest save's),
 * else (none recorded) a SIEM copy before its Sigma form. */
function mainRule(p: ProposalRow, listed: Map<number, Listed>): Listed | null {
  if (p.rule_id != null) return listed.get(p.rule_id) ?? null
  const rows = p.rule_ids.map((id) => listed.get(id)).filter((r): r is Listed => !!r)
  return rows.find((r) => r.language !== 'sigma') ?? rows[0] ?? null
}

/** A run that ended without saving a rule for the proposal: how it ended, else null. One
 * the backend still counts as working (its investigation not settled yet) has not ended. */
const endedBare = (p: ProposalRow) =>
  !p.rule_ids.length && p.run_id && p.run_status && !p.run_working ? RUN_ENDED[p.run_status] ?? null : null

/** Where a proposal stands, in the rule list's own health pill. */
function standing(p: ProposalRow, listed: Map<number, Listed> | null): { label: string; tone: string; gone?: boolean } {
  if (!p.rule_ids.length) {
    if (!p.run_id) return { label: 'Waiting for a run', tone: 'none' }
    const ended = endedBare(p)
    return ended ? { label: `No rule · the run ${ended}`, tone: 'idle', gone: true } : { label: 'No rule yet', tone: 'none' }
  }
  if (!listed) return { label: plural(p.rule_ids.length, 'rule', 'rules'), tone: 'none' }
  const rule = mainRule(p, listed)
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
  const [rulesError, setRulesError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Whether Vigil ever proposed one: an empty list after a dismiss is not "none yet".
  const [proposedAny, setProposedAny] = useState(false)

  // Only the copies the proposals follow, a share at a time: the listing caps its rows.
  const readRules = useCallback((props: ProposalRow[]) => {
    const ids = [...new Set(props.flatMap(followed))]
    if (!ids.length) { setListed(new Map()); setRulesError(null); return }
    const shares: number[][] = []
    for (let i = 0; i < ids.length; i += RULES_PER_READ) shares.push(ids.slice(i, i + RULES_PER_READ))
    Promise.all(shares.map((share) => detectionRulesApi.rules({ rules: share.join(',') })))
      .then((reads) => {
        setListed(new Map(reads.flatMap((res) => (res.data as { rules: Listed[] }).rules.map((r) => [r.rule_id, r] as const))))
        setRulesError(null)
      })
      .catch((e) => { setListed(null); setRulesError(errorText(e, 'no answer from the server')) })
  }, [])

  useEffect(() => {
    let live = true
    detectionEngineeringApi.proposals()
      .then((res) => {
        if (!live) return
        // Vigil's own drafts after a gap, and inventory rules a person proposed to prove; one
        // a person or Vigil rejected is off the list, unless a rule is attached to it: a reject
        // does not stop that rule's rollout.
        const gaps = res.data.proposals.filter((p) => p.origin?.source === GAPS_RUN || fromInventory(p))
        const mine = gaps.filter((p) => p.state !== 'rejected' || p.rule_ids.length > 0)
        setProposedAny(gaps.length > 0)
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
    if (proposedAny) {
      return (
        <Empty title="No proposed rule is open">
          Every rule Vigil proposed was dismissed or rejected. The daily gaps run drafts a new one here when a proven attack, alerts on a paper-only technique or recent threat intel show a technique no rule catches.
        </Empty>
      )
    }
    return (
      <Empty title="Vigil has not proposed a rule yet">
        The daily gaps run drafts one here when a proven attack, alerts on a paper-only technique or recent threat intel show a technique no rule catches.
      </Empty>
    )
  }

  const pickedId = params.get('proposal')
  const picked = rows.find((p) => p.proposal_id === pickedId) ?? rows[0]
  const pick = (id: string) => setParams((now) => {
    const next = new URLSearchParams(now)
    next.set('tab', 'proposed')
    next.set('proposal', id)
    return next
  }, { replace: true })
  const onKey = (e: KeyboardEvent<HTMLLIElement>, id: string) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(id) }
  }
  // A dismissed proposal is rejected, so off the list: the detail moves to the row after it.
  const dismissed = (id: string) => {
    const at = rows.findIndex((p) => p.proposal_id === id)
    const left = rows.filter((p) => p.proposal_id !== id)
    const next = left[at] ?? left[at - 1]
    setRows(left)
    setParams((now) => {
      const params = new URLSearchParams(now)
      if (next) params.set('proposal', next.proposal_id)
      else params.delete('proposal')
      return params
    }, { replace: true })
  }

  return (
    <div className="de-prop">
      <section className="x1-card de-card de-prop-list">
        <div className="de-card-head de-prop-list-head">
          <div>
            <h2>Proposed rules <span>· {rows.length.toLocaleString()}</span></h2>
            <p>Vigil’s drafts after a proven miss, and inventory rules proposed to prove</p>
          </div>
        </div>
        {rulesError && (
          <p className="de-card-sub">
            <span className="de-x-err" role="alert">Where each rule stands did not load: {rulesError}</span>{' '}
            <button type="button" className="x1-link" onClick={() => readRules(rows)}>Try again</button>
          </p>
        )}
        <ul className="list-scroll de-prop-rows" role="listbox" aria-label="Proposed rules">
          {rows.map((p) => {
            const st = standing(p, listed)
            const rule = listed ? mainRule(p, listed) : null
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
                  {fromInventory(p) ? `From the inventory · ${who(p.created_by)}` : p.run_id ? `Gaps run · run ${short(p.run_id)}` : 'Gaps run'} · {utcDayClock(p.created_at)}
                </div>
              </li>
            )
          })}
        </ul>
      </section>
      <ProposalDetail
        key={picked.proposal_id}
        id={picked.proposal_id}
        row={picked.rule_id != null ? listed?.get(picked.rule_id) ?? null : null}
        onChanged={() => readRules(rows)}
        onDismissed={() => dismissed(picked.proposal_id)}
      />
    </div>
  )
}

/** A copy's name: its language, and its rule id when another attached copy shares it. */
function copyName(r: RuleDetail, all: RuleDetail[]): string {
  return all.filter((o) => o.source === r.source).length > 1 ? `${lang(r.source)} ${r.rule_id}` : lang(r.source)
}

type Decision = 'switch-off' | 'revert' | 'dismiss'

/** What a person can decide about the rule, and what happens without them. ``ended`` is
 * how the run ended when it saved no rule for the proposal. */
function decisionLine(main: RuleDetail | null, row: Listed | null, canWrite: boolean, ended: string | null): string {
  if (ended) {
    const what = `The run ${ended} without saving a rule, so nothing runs for this gap, and a later gaps run can raise it again.`
    return canWrite
      ? `${what} Dismiss it to take it off this list; it stays on the record with your reason.`
      : `${what} A person who can edit detections can dismiss it.`
  }
  if (!main) return 'Its rule is no longer listed, so there is nothing left to decide here.'
  if (main.stage === 'live') {
    return canWrite
      ? 'Live: analysts get its alerts. Revert to shadow takes it out of production and holds it there until a person releases it on the Rules tab.'
      : 'Live: analysts get its alerts. A person who can edit detections can move it back to shadow.'
  }
  if (main.stage === 'off') return 'Switched off: it no longer runs, and a new version of it waits in shadow until a person releases it.'
  if (main.stage !== 'shadow') return 'Saved, with no rollout stage yet.'
  const stop = canWrite ? ' Switch it off to stop it.' : ' A person who can edit detections can switch it off.'
  if (row?.held) return `Held in shadow: it runs but alerts nobody, and stays there until a person releases it on the Rules tab.${stop}`
  const unproven = row ? unprovenNote(row) : null
  return `Rollout is automatic: in shadow it runs but alerts nobody${unproven
    ? `. ${unproven}`
    : ', and Vigil moves it live after 7 quiet days with its data arriving, once it has been shown to catch the attack it was written for.'}${stop}`
}

/** The picked proposal and the rule copies attached to it; a copy no longer listed is null.
 * ``row`` is the followed copy's rule listing row (its hold and why it stays in shadow). */
function ProposalDetail({ id, row, onChanged, onDismissed }: { id: string; row: Listed | null; onChanged: () => void; onDismissed: () => void }) {
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
  // The copy its stages follow (the latest save's); a removed one leaves none, never an older copy.
  const at = proposal.rule_id != null ? proposal.rule_ids.indexOf(proposal.rule_id) : -1
  const main = at >= 0 ? rules[at] ?? null : proposal.rule_id == null ? shown.find((r) => r.source !== 'sigma') ?? shown[0] ?? null : null
  const gap = proposal.origin.gap
  const attach = [...proposal.events].reverse().find((e) => e.evidence?.step === 'attach')
  // The rule's text by copy, [key, name, text]: each listed copy's latest query, else what the run saved.
  const texts: [string, string, string][] = shown.length
    ? shown.map((r) => [String(r.rule_id), copyName(r, shown), r.query])
    : [...Object.entries(proposal.copies), ...(proposal.sigma ? [['sigma', proposal.sigma] as [string, string]] : [])].map(([l, t]) => [l, lang(l), t])
  const langs = [...new Set(shown.length ? shown.map((r) => lang(r.source)) : texts.map(([, n]) => n))].join(' + ')
  const on = texts.find(([k]) => k === copy) ?? (main && texts.find(([k]) => k === String(main.rule_id))) ?? texts[0]
  // The main copy's moves: from its rule read, else (a removed copy) from the proposal's record.
  const moves = main?.stages ?? proposal.stages.filter((m) => m.rule_id === (proposal.rule_id ?? proposal.rule_ids[0]))
  const ended = endedBare(proposal)
  // Dismiss only once the run ended with no rule: a working run could still attach one, and
  // a reject does not stop a rule that is attached.
  const decision: Decision | null = !canWrite
    ? null
    : ended ? 'dismiss' : !main ? null : main.stage === 'shadow' ? 'switch-off' : main.stage === 'live' ? 'revert' : null
  const bare = proposal.run_id
    ? ended ? `The run ${ended} without saving a rule for this gap` : 'The run has not saved a rule for this gap yet'
    : 'No rule until a run drafts one'
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
                attach
                  ? `drafted by ${who(attach.actor)}, ${utcDayClock(attach.at)}`
                  : fromInventory(proposal)
                    ? `proposed from the inventory by ${who(proposal.created_by)}, ${utcDayClock(proposal.created_at)}`
                    : `raised by the gaps run, ${utcDayClock(proposal.created_at)}`,
              ].filter(Boolean).join(' · ')}
            </p>
          </div>
          {((decision && !asking) || proposal.technique) && (
            <div className="de-prop-acts">
              {proposal.technique && (
                <Emulate key={proposal.proposal_id} className="btn ghost" goal={`Test the gap of proposal ${proposal.proposal_id} on ${proposal.technique}${main ? `, answered by rule ${main.rule_id} "${main.title}" version ${main.version}` : ', which has no rule yet'}: emulate ${proposal.technique} on a listed host and read whether ${main ? 'that version' : 'any rule'} caught it.`} />
              )}
              {decision && !asking && (
                <button type="button" className="btn" onClick={() => setAsking(true)}>{ASK[decision].button}</button>
              )}
            </div>
          )}
        </div>
        {(proposal.rule_ids.length > 0 || ended) && <p className="de-card-sub">{decisionLine(main, row, canWrite, ended)}</p>}
        {decision === 'dismiss' && asking && (
          <Decide action={decision} act={(reason) => detectionEngineeringApi.reject(proposal.proposal_id, reason)} onDone={(done) => (done ? onDismissed() : setAsking(false))} />
        )}
        {decision && decision !== 'dismiss' && asking && main && (
          <Decide
            action={decision}
            act={(reason) => (decision === 'switch-off' ? detectionRulesApi.switchOffRule : detectionRulesApi.revertRule)(main.rule_id, reason)}
            onDone={(done) => (done ? changed() : setAsking(false))}
          />
        )}
      </section>

      <section className="x1-card de-card">
        <div className="de-card-head">
          <div>
            <h2>{fromInventory(proposal) ? 'Why it was proposed' : 'Why Vigil proposed it'}</h2>
            <p>{fromInventory(proposal) ? 'A person proposed an inventory rule to prove it' : 'From the gap to the rule'}</p>
          </div>
        </div>
        <WhyChain proposal={proposal} gap={gap} main={main} stage={moves.length ? moves[moves.length - 1].stage : null} langs={langs} bare={bare} attachAt={attach?.at ?? null} attachBy={attach?.actor ?? null} />
      </section>

      <section className="x1-card de-card">
        <div className="de-card-head">
          <div>
            <h2>The rule</h2>
            <p>{main?.description || (texts.length ? 'What the run saved for this gap' : bare)}</p>
          </div>
          {texts.length > 1 && (
            <div className="de-prop-copies" role="group" aria-label="Rule copy">
              {texts.map(([k, name]) => (
                <button key={k} type="button" aria-pressed={on?.[0] === k} className={`btn ${on?.[0] === k ? 'primary' : 'ghost'}`} onClick={() => setCopy(k)}>{name}</button>
              ))}
            </div>
          )}
        </div>
        {on && <pre className="de-raw de-prop-query">{on[2]}</pre>}
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

function WhyChain({ proposal, gap, main, stage, langs, bare, attachAt, attachBy }: {
  proposal: Proposal
  gap: ProposalGap | undefined
  main: RuleDetail | null
  /** The main copy's latest stage, read or recorded. */
  stage: Stage | null
  langs: string
  /** What stands in for the rule while none is attached. */
  bare: string
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
        <b className={kind?.tone}>{kind?.word ?? (fromInventory(proposal) ? 'Inventory' : '—')}</b>
        <span className="de-rollout-line">
          {gap
            ? `${gap.technique}: ${gapLine(gap)}`
            : fromInventory(proposal)
              ? `Rule ${String(proposal.origin.rule_id)}${proposal.origin.rule_version != null ? ` version ${String(proposal.origin.rule_version)}` : ''}, proposed to prove it`
              : proposal.title}
        </span>
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
                  ? <Link className="de-link" to={`/cases?case=${encodeURIComponent(ref)}`}>{caseLabel(ref)}</Link>
                  : what === 'hunt'
                    ? <HuntRef id={ref} run={proposal.hunts?.[ref]} />
                    : <>{what} <span className="mono">{ref}</span></>}
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
        <span className="de-rollout-line">
          {proposal.run_id
            ? `Vigil handed the gap to an agent run to draft a rule${proposal.run_working ? ' (still working)' : proposal.run_status ? ` (${RUN_ENDED[proposal.run_status] ?? proposal.run_status})` : ''}`
            : 'Not handed to a run yet'}
        </span>
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
            : bare}
        </span>
      </li>
    </ul>
  )
}

const passWord = (c: RuleCheck | undefined, pass: string, fail: string) =>
  !c?.result ? null : c.result === 'pass' ? pass : c.result === 'fail' ? fail : 'Not known'

/** A replay's result in words. A pass means it matched at least one of the proven events it
 * was given (not each of them) and none of the ruled-out ones. */
function replayWord(c: RuleCheck): string {
  if (c.result !== 'pass') return c.result === 'fail' ? 'Missed' : 'Not known'
  const proven = Number(c.proven_events ?? 0)
  const out = Number(c.ruled_out_events ?? 0)
  return `Matched ${proven > 1 ? `at least 1 of ${proven} proven events` : 'the proven event'}${out ? `, cleared ${out} ruled out` : ''}`
}

/** Where a proposal replay's attack events came from, in words: ``attack_basis`` is
 * "linked to hunt:…, case:…", "named by a person" or "none". */
function attackFrom(basis: string): string {
  if (!basis.startsWith('linked to ')) return basis === 'named by a person' ? 'events a person named' : 'no attack events: the gap names no proven hunt or Case'
  const refs = basis.slice('linked to '.length).split(', ').map((k) => {
    // A Case this reader can't read reaches here as "a case".
    if (!k.includes(':')) return 'a Case you can’t read'
    const [what, ref] = [k.slice(0, k.indexOf(':')), k.slice(k.indexOf(':') + 1)]
    return what === 'case' ? caseLabel(ref) : `${what === 'hunt' ? 'Hunt' : what} ${ref}`
  })
  return `the hosts and window of ${refs.join(', ')}`
}

/** Vigil's own replay of the proposal's Sigma text on the SIEMs (the proposal's latest
 * replay step), beside the save-time proof of each copy. */
function proposalReplay(proposal: Proposal): { row: { what: string; said: string; title?: string }; note: string } | null {
  const step = [...proposal.events].reverse().find((e) => e.evidence?.step === 'replay')
  if (!step) return null
  const ev = step.evidence as { attack_basis?: string; hits?: number; attack_rows?: number; false_positives?: number; proven?: boolean; evaluated?: boolean; unproven_because?: string | null }
  const counts = ev.evaluated === false
    ? ''
    : `Hit ${Number(ev.hits ?? 0).toLocaleString()} of ${plural(Number(ev.attack_rows ?? 0), 'attack event', 'attack events')}, ${plural(Number(ev.false_positives ?? 0), 'false positive', 'false positives')}. `
  const stale = step.judges_current_text === false ? ' It judged an earlier text of the rule.' : ''
  return {
    row: { what: 'Replay · Sigma', said: ev.evaluated === false ? 'Not run' : ev.proven ? 'Proven' : 'Not proven', title: utcDayClock(step.at) ?? undefined },
    note: `${counts}Attack events from ${attackFrom(ev.attack_basis ?? 'none')}; replayed by ${who(step.actor)}, ${utcDayClock(step.at)}.${ev.unproven_because ? ` ${ev.unproven_because}` : ''}${stale}`,
  }
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
    const name = copyName(r, shown)
    const lint = passWord(r.checks.lint, 'Passed', 'Failed')
    if (lint) before.push({ what: `Lint · ${name}`, said: lint, title: r.checks.lint?.reason ?? undefined })
    const replay = r.checks.replay
    if (replay?.result) before.push({ what: `Replay · ${name}`, said: replayWord(replay), title: replay.reason ?? undefined })
  }
  const replayed = proposalReplay(proposal)
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
      {replayed && (
        <>
          <Group title="On the proven attack" rows={[replayed.row]} />
          <p className="de-card-sub">{replayed.note}</p>
        </>
      )}
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

/** Each decision's words: its button, what follows it, the reason's prompt and its failure. */
const ASK: Record<Decision, { button: string; note: string; ask: string; failed: string; tone: string }> = {
  'switch-off': {
    button: 'Switch off',
    note: 'Vigil stops running it, and a later version waits in shadow for a person.',
    ask: 'Why switch it off (required)',
    failed: 'Could not switch it off.',
    tone: 'danger',
  },
  revert: {
    button: 'Revert to shadow',
    note: 'Analysts stop getting its alerts; it keeps running in shadow until a person releases it.',
    ask: 'Why move it back to shadow (required)',
    failed: 'Could not revert it.',
    tone: 'primary',
  },
  dismiss: {
    button: 'Dismiss',
    note: 'It leaves this list as rejected; your reason stays on its record.',
    ask: 'Why dismiss it (required)',
    failed: 'Could not dismiss it.',
    tone: 'danger',
  },
}

/** A refusal's message (a 422 sends it under ``detail.message``), else the usual text. */
function refusal(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: { message?: unknown } } } })?.response?.data?.detail
  return typeof detail?.message === 'string' && detail.message ? detail.message : errorText(err, fallback)
}

/** Switch off (shadow), Revert to shadow (live) or Dismiss (a proposal whose run ended with
 * no rule): the reason is required and stays on the record; ``ASK`` says what follows each. */
function Decide({ action, act, onDone }: { action: Decision; act: (reason: string) => Promise<unknown>; onDone: (changed: boolean) => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ask = ASK[action]
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!reason.trim()) { setError('Say why.'); return }
    setBusy(true)
    act(reason.trim())
      .then(() => onDone(true))
      .catch((err) => { setError(refusal(err, ask.failed)); setBusy(false) })
  }
  return (
    <form className="de-x-form de-prop-ask" onSubmit={submit}>
      <span className="de-card-sub">{ask.note}</span>
      <input className="de-x-in" aria-label="Reason" placeholder={ask.ask} value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      {error && <span className="de-x-err" role="alert">{error}</span>}
      <div className="de-x-actions">
        <button type="submit" className={`btn de-x-btn ${ask.tone}`} disabled={busy}>{busy ? 'Saving…' : ask.button}</button>
        <button type="button" className="btn ghost de-x-btn" onClick={() => onDone(false)}>Cancel</button>
      </div>
    </form>
  )
}
