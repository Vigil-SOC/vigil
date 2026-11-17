/* The investigation row's replay: start it on the row's model and show the
   latest result. The assignment itself saves on select, elsewhere. */
import { useEffect, useRef, useState } from 'react'
import { aiConfigApi, type InvestigationReplayResponse } from '../../services/api'
import { agreementText, falseCloseText } from '../../shared/autonomy'
import { fmtCost } from '../../shared/cost'
import { NotMeasured } from '../../shared/NotMeasured'

const POLL_MS = 2_000
const FALSE_CLOSE_SHOWN = 3

const REPLAY_STATUS = ['running', 'failed', 'succeeded'] as const
type ReplayStatus = (typeof REPLAY_STATUS)[number]

const CLOSURE_LABEL: Record<string, string> = {
  resolved: 'Resolved',
  false_positive: 'False positive',
  duplicate: 'Duplicate',
  unable_to_resolve: 'Unable to resolve',
}

function replayStatus(status: string): ReplayStatus | null {
  return (REPLAY_STATUS as readonly string[]).includes(status) ? (status as ReplayStatus) : null
}

function httpStatus(e: unknown): number | undefined {
  return (e as { response?: { status?: number } })?.response?.status
}

function apiDetail(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' && detail ? detail : fallback
}

function money(usd: number): string {
  const digits = usd !== 0 && Math.abs(usd) < 0.01 ? 4 : 2
  return fmtCost(usd, undefined, digits)
}

function evidenceText(opened: boolean | null | undefined): string {
  if (opened === true) return 'evidence opened'
  if (opened === false) return 'evidence not opened'
  return 'evidence not recorded'
}

/** agreementText's "N reviews · X% agree", plus the replay's Wilson interval. */
function agreementLine(row: InvestigationReplayResponse): string | null {
  const base = agreementText({
    agreed: row.agreed,
    disagreed: row.disagreed,
    agreement_rate: row.agreement_rate ?? null,
  })
  if (!base) return null
  const low = row.agreement_wilson_low
  const high = row.agreement_wilson_high
  if (low == null || high == null) return base.text
  return `${base.text} · ${Math.round(low * 100)}–${Math.round(high * 100)}%`
}

function falseCloseLine(row: InvestigationReplayResponse): string | null {
  // falseCloseText reads answered / wilson_*; the replay names them replayed / false_close_wilson_*.
  return falseCloseText({
    false_close_rate: row.false_close_rate ?? null,
    answered: row.replayed,
    wilson_low: row.false_close_wilson_low ?? null,
    wilson_high: row.false_close_wilson_high ?? null,
  })
}

function RunningCounts({ row }: { row: InvestigationReplayResponse }) {
  return (
    <p className="aim-replay-line">
      {row.replayed} replayed · {row.skipped} skipped · {row.false_closes} {row.false_closes === 1 ? 'false close' : 'false closes'} · {row.total} total · {money(row.cost_usd)}
    </p>
  )
}

function SkippedAndCost({ row }: { row: InvestigationReplayResponse }) {
  return (
    <>
      <p className="aim-replay-line">Skipped {row.skipped}</p>
      <p className="aim-replay-line">{money(row.cost_usd)}</p>
    </>
  )
}

function FalseCloses({ items }: { items: InvestigationReplayResponse['false_close'] }) {
  const [more, setMore] = useState(false)
  if (items.length === 0) return null
  const shown = more ? items : items.slice(0, FALSE_CLOSE_SHOWN)
  const rest = items.length - shown.length
  return (
    <ul className="aim-replay-cases">
      {shown.map((item, i) => (
        <li key={`${item.case_id}-${i}`}>
          <span className="aim-replay-id">{item.case_id}</span>
          {' · '}
          {CLOSURE_LABEL[item.closure_category] ?? item.closure_category}
          {' · '}
          {item.disposition}
          {' · '}
          {evidenceText(item.evidence_opened)}
        </li>
      ))}
      {rest > 0 && (
        <li>
          <button type="button" className="aim-replay-more" onClick={() => setMore(true)}>
            +{rest}
          </button>
        </li>
      )}
    </ul>
  )
}

function Finished({ row }: { row: InvestigationReplayResponse }) {
  if (row.total === 0) {
    return (
      <>
        <p className="aim-replay-line">There were no qualifying cases.</p>
        <SkippedAndCost row={row} />
      </>
    )
  }
  return (
    <>
      <p className="aim-replay-line">{falseCloseLine(row) ?? <NotMeasured label="False close" />}</p>
      <FalseCloses items={row.false_close} />
      <p className="aim-replay-line">{agreementLine(row) ?? <NotMeasured label="Agreement" />}</p>
      <SkippedAndCost row={row} />
    </>
  )
}

function ReplayResult({ row }: { row: InvestigationReplayResponse }) {
  const status = replayStatus(row.status)
  if (status === null) return <p className="aim-replay-line">{row.status}</p>
  switch (status) {
    case 'running':
      return <RunningCounts row={row} />
    case 'failed':
      return <p className="aim-replay-reason">{row.reason || 'Replay failed.'}</p>
    case 'succeeded':
      return <Finished row={row} />
    default: {
      const unreachable: never = status
      return unreachable
    }
  }
}

export function InvestigationReplay({ providerId, modelId }: { providerId: string; modelId: string }) {
  const [row, setRow] = useState<InvestigationReplayResponse | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const readGen = useRef(0)
  const startGen = useRef(0)
  // Bumps whenever a payload is applied, so a 404 from a read that started earlier cannot clear it.
  const epoch = useRef(0)
  const rowRef = useRef(row)
  rowRef.current = row

  const take = (next: InvestigationReplayResponse) => {
    epoch.current += 1
    // A read that started before this replay must not paint over it.
    setRow((cur) => (cur && next.started_at < cur.started_at ? cur : next))
    setNote(null)
  }

  const read = () => {
    const mine = ++readGen.current
    const started = startGen.current
    const seen = epoch.current
    aiConfigApi
      .getInvestigationReplay()
      .then((res) => {
        if (mine !== readGen.current || started !== startGen.current) return
        take(res.data)
      })
      .catch((e: unknown) => {
        if (mine !== readGen.current || started !== startGen.current) return
        if (httpStatus(e) === 404) {
          if (epoch.current !== seen) return
          setRow(null)
          setNote(null)
          return
        }
        if (!rowRef.current) setNote(apiDetail(e, 'Could not load the replay.'))
      })
  }

  useEffect(() => {
    let live = true
    const tick = () => {
      if (!live || document.visibilityState === 'hidden') return
      read()
    }
    tick()
    const id = window.setInterval(() => {
      if (rowRef.current?.status === 'running') tick()
    }, POLL_MS)
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return
      // A hidden first load never read; a running replay keeps moving while the tab is away.
      if (!rowRef.current || rowRef.current.status === 'running') read()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      live = false
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
    // read closes over the latest gens; the effect runs once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const running = row?.status === 'running'
  const canStart = !!providerId && !!modelId && !running && !starting

  const start = async () => {
    if (!providerId || !modelId || starting || running) return
    setStarting(true)
    const mine = ++startGen.current
    readGen.current += 1
    try {
      const res = await aiConfigApi.startInvestigationReplay({ provider_id: providerId, model_id: modelId })
      if (mine === startGen.current) take(res.data)
    } catch (e: unknown) {
      if (mine !== startGen.current) return
      // A replay is already running: keep whatever is on screen and refresh it.
      if (httpStatus(e) === 409) {
        read()
        return
      }
      setNote(apiDetail(e, 'Could not start the replay.'))
    } finally {
      if (mine === startGen.current) setStarting(false)
    }
  }

  return (
    <div className="aim-replay">
      <div className="aim-replay-top">
        <button type="button" className="btn ghost" disabled={!canStart} onClick={() => void start()}>
          {running || starting ? 'Replaying…' : 'Replay cases'}
        </button>
        {!providerId || !modelId ? <span className="aim-note">Assign a model before replaying.</span> : null}
      </div>
      {note && <p className="aim-replay-reason">{note}</p>}
      {row && <ReplayResult key={`${row.started_at}:${row.status}`} row={row} />}
    </div>
  )
}
