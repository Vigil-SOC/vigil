import { Fragment } from 'react'
import { Icon } from '../../shared/icons'
import { EmptyState } from '../../shared/ui'
import { checkKeys, REPO_TOOLS, uncheckedReason, useRules, type CheckResult, type CheckTally, type RulesHealth } from './useRules'

const RESULTS = ['pass', 'fail', 'unknown', 'unchecked']

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

/** A check result as a pill: fail is a risk in the estate, the rest stay neutral. */
function CheckPill({ result }: { result: string }) {
  const r = RESULTS.includes(result) ? result : 'unchecked'
  return (
    <span className={`check-pill ${r}`}>
      <span className="state-dot" aria-hidden="true" />
      {cap(r)}
    </span>
  )
}

/** Reason text that wraps and stops at two lines, the full text on hover. */
function Reason({ text }: { text?: string | null }) {
  if (!text) return null
  return <span className="text-xs text-tx-2 line-clamp-2 break-words" title={text}>{text}</span>
}

/** "208 pass · 3 fail · 2 waiting for the query budget": unchecked reads as what it means for this tool */
function tallyLine(t: CheckTally, unchecked: string): string {
  return RESULTS.filter((r) => t[r]).map((r) => `${t[r]} ${r === 'unchecked' ? unchecked : r}`).join(' · ') || 'no rules'
}

function ToolKpi({ tool, h }: { tool: string; h: RulesHealth }) {
  const t = h.by_tool[tool]
  if (REPO_TOOLS.has(tool)) {
    return (
      <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-b border-line-soft min-w-0">
        <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">{cap(tool)}</span>
        <div className="flex items-baseline gap-2.5"><span className="text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]">{t.rules}</span><span className="text-xs text-tx-3">rules</span></div>
        <span className="text-xs text-tx-faint">repo side · deployed through a SIEM</span>
      </div>
    )
  }
  const failing = h.not_passing.by_tool[tool]?.failing ?? 0
  return (
    <div className="relative flex flex-col gap-[3px] px-[22px] py-4 border-r border-b border-line-soft min-w-0">
      <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3 truncate">{cap(tool)} · {plural(t.rules, 'rule')}</span>
      <div className="flex items-baseline gap-2.5">
        <span className={`text-[30px] font-semibold tracking-[-0.02em] leading-[1.1]${failing ? ' text-crit' : ''}`}>{failing}</span>
        <span className="text-xs text-tx-3">can’t fire</span>
      </div>
      {checkKeys(t).map((k) => {
        const line = tallyLine(t[k] as CheckTally, uncheckedReason(h, k, tool))
        return (
          <span key={k} className="text-xs text-tx-faint line-clamp-2 break-words" title={`${cap(k)}: ${line}`}>
            <span className="text-tx-3 font-semibold">{cap(k)}</span> {line}
          </span>
        )
      })}
    </div>
  )
}

function BlindSpotsCard({ h }: { h: RulesHealth }) {
  const spots = h.blind_spots
  return (
    <div className="bg-panel border border-line rounded-lg shadow-panel overflow-hidden">
      <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft">
        <h3 className="text-[14.5px]">Blind spots</h3>
        <span className="flex-1" />
        <span className="text-xs text-tx-3">data sources not arriving, and the rules they starve</span>
      </div>
      {spots.length === 0 ? (
        <EmptyState compact icon="eye" title="No blind spots" body="Every data source a checked rule reads is arriving." />
      ) : (
        <div className="table-wrap">
          <table className="tbl rules-tbl">
            <thead><tr><th className="w-[34%]">Rule</th><th className="w-[110px]">Tool</th><th>Reason</th></tr></thead>
            <tbody>
              {spots.map((s) => (
                <Fragment key={s.data_source}>
                  <tr className="rules-group">
                    <td colSpan={3}>
                      <div className="flex items-baseline gap-2 min-w-0">
                        <span className="mono text-[12.5px] break-all line-clamp-2 min-w-0" title={s.data_source}>{s.data_source}</span>
                        <span className="text-xs text-tx-3 whitespace-nowrap">{plural(s.rules.length, 'rule')} starved</span>
                      </div>
                    </td>
                  </tr>
                  {s.rules.map((r) => (
                    <tr key={r.rule_id}>
                      <td><span className="line-clamp-2 break-words" title={r.title}>{r.title}</span></td>
                      <td>{cap(r.tool)}</td>
                      <td><Reason text={r.reason} /></td>
                    </tr>
                  ))}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function CheckCell({ h, tool, check, c }: { h: RulesHealth; tool: string; check: string; c?: CheckResult }) {
  const result = c?.result && RESULTS.includes(c.result) ? c.result : 'unchecked'
  const reason = result === 'unchecked' ? uncheckedReason(h, check, tool) : c?.reason
  return (
    <div className="flex flex-col items-start gap-1 min-w-0">
      <span title={c?.checked_at ? `checked ${c.checked_at}` : undefined}><CheckPill result={result} /></span>
      <Reason text={reason} />
    </div>
  )
}

function NotPassingCard({ h }: { h: RulesHealth }) {
  const { rules, total } = h.not_passing
  // check columns: every check a SIEM tool tallies, plus any a listed rule carries
  const checks = [...new Set([
    ...Object.entries(h.by_tool).filter(([t]) => !REPO_TOOLS.has(t)).flatMap(([, t]) => checkKeys(t)),
    ...rules.flatMap((r) => Object.keys(r.checks)),
  ])]
  return (
    <div className="bg-panel border border-line rounded-lg shadow-panel overflow-hidden">
      <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft">
        <h3 className="text-[14.5px]">Rules that can’t fire</h3>
        <span className="flex-1" />
        <span className="text-xs text-tx-3">
          {total > rules.length ? `first ${rules.length} of ${total}` : plural(total, 'rule')} failing or unknown on a check · fails first
        </span>
      </div>
      {rules.length === 0 ? (
        <EmptyState compact icon="check" title="Every checked rule passes" body="No active SIEM rule fails or is unknown on a check." />
      ) : (
        <div className="table-wrap">
          <table className="tbl rules-tbl rules-checks">
            <thead>
              <tr>
                <th className="w-[90px]">Tool</th>
                <th className="w-[18%]">Rule</th>
                <th className="w-[72px]">Version</th>
                {checks.map((k) => <th key={k}>{cap(k)}</th>)}
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.rule_id}>
                  <td>{cap(r.tool)}</td>
                  <td>
                    <span className="line-clamp-2 break-words" title={r.title}>{r.title}</span>
                    <span className="mono text-[11px] text-tx-3 block truncate" title={r.id}>{r.id}</span>
                  </td>
                  <td><span className="mono">v{r.version}</span></td>
                  {checks.map((k) => <td key={k}><CheckCell h={h} tool={r.tool} check={k} c={r.checks[k]} /></td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {total > rules.length && (
        <div className="px-[18px] py-2 border-t border-line-soft text-xs text-tx-3">
          {total - rules.length} more not shown. Ask Vigil can search them by tool and Runs or Sees result.
        </div>
      )}
    </div>
  )
}

export default function RulesTab() {
  const { data: h, phase, error, reload } = useRules()
  const tools = h ? Object.keys(h.by_tool).sort((a, b) => Number(REPO_TOOLS.has(a)) - Number(REPO_TOOLS.has(b)) || a.localeCompare(b)) : []
  const siems = tools.filter((t) => !REPO_TOOLS.has(t))

  return (
    <>
      {h && tools.length > 0 && (
        <div className="grid border-b border-line rules-kpis">
          {tools.map((t) => <ToolKpi key={t} tool={t} h={h} />)}
        </div>
      )}

      <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line">
        <span className="text-[13px] text-tx-2">
          {h ? (h.repo.rules ? `${plural(h.repo.not_deployed, 'repo rule')} not deployed to any SIEM` : 'No repo rules') : ' '}
          {h && h.repo.rules > 0 && <span className="text-tx-3"> · {h.repo.deployed} of {h.repo.rules} have a linked copy</span>}
        </span>
        <div className="flex-1" />
        <button className="btn ghost icon" title="Refresh" onClick={reload}><Icon name="refresh" /></button>
      </div>

      {phase === 'loading' && !h && <EmptyState loading icon="shield" title="Loading rule health…" />}
      {phase === 'error' && (
        <EmptyState error icon="alert" title="Couldn’t load rule health" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />
      )}
      {h && siems.length === 0 && (
        <EmptyState icon="shield" title="No SIEM rules read yet" body="Rules from Kibana, Splunk or Wazuh appear here, with whether each can fire, after the next detection rule sync." />
      )}
      {h && siems.length > 0 && (
        <div className="flex flex-col gap-4 px-[22px] pt-5 pb-6">
          <BlindSpotsCard h={h} />
          <NotPassingCard h={h} />
          {h.unchecked.sees && siems.some((t) => !h.unchecked.sees.tools.includes(t)) && (
            <span className="text-xs text-tx-3">{h.not_read}.</span>
          )}
        </div>
      )}
    </>
  )
}
