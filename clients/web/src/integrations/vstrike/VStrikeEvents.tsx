import { useEffect, useState } from 'react'
import { vstrikeApi, type VStrikeFault } from './api'
import { sourceTime } from '../../data/findingPresentation'
import { Icon } from '../../shared/icons'

export default function VStrikeEvents({ storylineId, onFocus }: { storylineId: string; onFocus: (ips: string[]) => void }) {
  const [faults, setFaults] = useState<VStrikeFault[]>([])
  const [checked, setChecked] = useState<string>()
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(true)
  const [truncated, setTruncated] = useState(false)
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    setFaults([]); setChecked(undefined); setError(false); setLoading(true); setTruncated(false)
    const poll = async () => {
      try {
        const { data } = await vstrikeApi.faults(storylineId)
        if (cancelled) return
        setFaults(data.faults); setChecked(data.fetched_at); setTruncated(data.truncated); setError(false)
      } catch {
        if (!cancelled) setError(true)
      } finally {
        if (!cancelled) { setLoading(false); timer = setTimeout(() => { void poll() }, 5_000) }
      }
    }
    if (storylineId) void poll()
    else setLoading(false)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [storylineId])
  return <section aria-label="VStrike scenario events">
    <p className="muted">External simulation events. These are separate from Vigil findings and model scores.</p>
    {!storylineId ? <p>Choose a network and storyline above to read events.</p> : <>
      <p role="status">{loading ? 'Reading VStrike events…' : `${faults.length} scenario event${faults.length === 1 ? '' : 's'}`}{truncated && ' · latest page only'}</p>
      {checked && <p className="muted">Last checked {sourceTime(checked)}</p>}
      {error && <p role="alert">Refresh failed.{checked ? ' Showing the last successful read.' : ' Try reconnecting to VStrike.'}</p>}
      {!loading && !error && !faults.length && <p>No danger events were returned for this storyline.</p>}
      <div className="vstrike-event-list">{faults.map((fault) => <article key={fault.event_id}>
        <h3>{fault.label || 'VStrike event'}</h3>
        <time>{sourceTime(fault.occurred_at)}</time>
        <p>{fault.description}</p>
        {fault.source_level && <p className="muted">VStrike level: {fault.source_level}</p>}
        <div className="vstrike-event-action"><span className="mono">{fault.ip4s.join(', ') || 'No IPv4 address'}</span>
          <button className="btn ghost" disabled={!fault.ip4s.length} onClick={() => onFocus(fault.ip4s)}><Icon name="graph" size={14} />Open in graph</button></div>
        <details><summary>Measurement details</summary><dl>{Object.entries(fault.measurement).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl></details>
      </article>)}</div>
    </>}
  </section>
}
