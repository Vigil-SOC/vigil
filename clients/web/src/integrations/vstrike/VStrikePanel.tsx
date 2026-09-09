import { useCallback, useEffect, useRef, useState } from 'react'
import { useExtensions } from '../../extensions/ExtensionProvider'
import { EmptyState } from '../../shared/ui'
import { Icon } from '../../shared/icons'
import FindingsDrawer from '../../screens/dashboard/FindingsDrawer'
import VStrikeEvents from './VStrikeEvents'
import { actionError, options, vstrikeApi, type VStrikeOption } from './api'

export interface GraphRequest { id: number; ips: string[]; findingId?: string }
interface Props {
  active: boolean
  request: GraphRequest | null
  eventsOpen: boolean
  onCloseEvents: () => void
  onOpenEvents: () => void
  onFocus: (ips: string[]) => void
  onBack: () => void
  onBackToFinding: (id: string) => void
  onConfigure: () => void
}

export default function VStrikePanel(props: Props) {
  const { active, request, eventsOpen, onCloseEvents, onConfigure } = props
  const { enabledIntegrations, loading: integrationsLoading } = useExtensions()
  const enabled = !integrationsLoading && enabledIntegrations.includes('vstrike')
  const [connection, setConnection] = useState(0)
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')
  const [url, setUrl] = useState<string>()
  const [loaded, setLoaded] = useState(false)
  const [networks, setNetworks] = useState<VStrikeOption[]>([])
  const [network, setNetwork] = useState('')
  const [networkLoaded, setNetworkLoaded] = useState('')
  const [networkAttempt, setNetworkAttempt] = useState(0)
  const [storylines, setStorylines] = useState<VStrikeOption[]>([])
  const [storyline, setStoryline] = useState('')
  const [storylinesLoading, setStorylinesLoading] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [selection, setSelection] = useState<'idle' | 'pending' | 'requested' | 'error'>('idle')
  const [fullscreen, setFullscreen] = useState(false)
  const iframe = useRef<HTMLIFrameElement>(null)
  const surface = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(1280)
  const guard = `${connection}:${network}:${request?.id}:${active}:${enabled}`
  const currentGuard = useRef(guard)
  currentGuard.current = guard
  const autoFocus = useRef('')

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    setPhase('loading'); setUrl(undefined); setLoaded(false); setNetworkLoaded(''); setError(''); setSelection('idle'); setBusy(false)
    Promise.all([vstrikeApi.connect(), vstrikeApi.networks()]).then(([token, list]) => {
      if (cancelled) return
      const candidate = new URL(token.data.iframe_url)
      if (!['http:', 'https:'].includes(candidate.protocol) || candidate.username || candidate.password) throw new Error('Invalid iframe URL')
      const next = options(list.data.networks, ['networkId', 'id', 'network_id'])
      setUrl(candidate.href); setNetworks(next)
      setNetwork((previous) => next.some((item) => item.id === previous) ? previous : next.length === 1 ? next[0].id : '')
      setPhase('ready')
    }).catch(() => { if (!cancelled) { setPhase('error'); setError(actionError('connect to VStrike')) } })
    return () => { cancelled = true }
  }, [connection, enabled])

  useEffect(() => {
    setStorylines([]); setStoryline(''); setNetworkLoaded(''); setSelection('idle'); setBusy(false)
    if (!enabled || !network) { setStorylinesLoading(false); return }
    let cancelled = false
    setStorylinesLoading(true)
    vstrikeApi.storylines(network).then(({ data }) => {
      if (cancelled) return
      const list = options(data.storylines, ['storylineSetId', 'id', 'storylineId', 'storyline_id'])
      setStorylines(list); setStoryline(list.length === 1 ? list[0].id : '')
    }).catch(() => { if (!cancelled) setError(actionError('list storylines')) })
      .finally(() => { if (!cancelled) setStorylinesLoading(false) })
    return () => { cancelled = true }
  }, [network, enabled, connection])

  useEffect(() => {
    if (!url || !loaded || !network || !enabled) return
    let cancelled = false
    let confirmed = false
    const load = async () => {
      try {
        await vstrikeApi.loadNetwork(network)
        if (!cancelled) { setNetworkLoaded(network); setNetworkAttempt((value) => value + 1) }
      } catch { if (!cancelled) setError(actionError('load the network')) }
    }
    // iframe load can precede the provider's WebSocket registration. Retry once.
    const timers = [1_000, 12_000].map((delay) => setTimeout(() => { if (!confirmed) void load() }, delay))
    const onMessage = (event: MessageEvent) => {
      if (event.source !== iframe.current?.contentWindow || event.origin !== new URL(url).origin) return
      if (event.data?.type === 'vstrike:state' && event.data.networkId === network) {
        confirmed = true; timers.forEach(clearTimeout); setNetworkLoaded(network)
      }
    }
    window.addEventListener('message', onMessage)
    return () => { cancelled = true; timers.forEach(clearTimeout); window.removeEventListener('message', onMessage) }
  }, [url, loaded, network, enabled])

  useEffect(() => {
    if (!active) setFullscreen(false)
    const target = surface.current
    if (!active || !target) return
    const observer = new ResizeObserver(([entry]) => { if (entry.contentRect.width > 0) setWidth(entry.contentRect.width) })
    observer.observe(target)
    return () => observer.disconnect()
  }, [active, phase, fullscreen])

  const select = useCallback(async () => {
    if (!request || !network || !loaded || !enabled) return
    const started = currentGuard.current
    setSelection('pending'); setError('')
    try {
      await vstrikeApi.focus(network, request.ips)
      if (started === currentGuard.current) setSelection('requested')
    } catch {
      if (started === currentGuard.current) { setSelection('error'); setError(actionError('request the selection')) }
    }
  }, [request, network, loaded, enabled])

  useEffect(() => {
    if (!active || !request || !network || networkLoaded !== network || !loaded) return
    const key = `${connection}:${network}:${networkAttempt}:${request.id}`
    if (autoFocus.current === key) return
    autoFocus.current = key
    void select()
  }, [active, request, networkLoaded, network, loaded, connection, networkAttempt, select])

  useEffect(() => {
    if (!active) { setSelection((value) => value === 'pending' ? 'idle' : value); setBusy(false) }
  }, [active])

  const action = async (call: () => Promise<unknown>, label: string) => {
    const started = currentGuard.current
    setBusy(true); setError('')
    try { await call() } catch { if (started === currentGuard.current) setError(actionError(label)) }
    finally { if (started === currentGuard.current) setBusy(false) }
  }
  const reconnect = () => {
    setLoaded(false); setNetworkLoaded(''); setSelection('idle'); setBusy(false)
    setConnection((value) => value + 1)
  }
  const controls = <div className="vstrike-controls">
    <label>Network<select aria-label="VStrike network" value={network} disabled={busy || selection === 'pending'} onChange={(event) => setNetwork(event.target.value)}>
      <option value="">{networks.length ? 'Choose network' : 'No networks available'}</option>{networks.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
    </select></label>
    <label>Storyline<select aria-label="VStrike storyline" value={storyline} disabled={!network || storylinesLoading || busy} onChange={(event) => setStoryline(event.target.value)}>
      <option value="">{storylinesLoading ? 'Loading storylines…' : 'Choose storyline'}</option>{storylines.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
    </select></label>
  </div>
  const settings = <EmptyState icon="graph" title={integrationsLoading ? 'Checking VStrike configuration…' : 'Connect VStrike'}
    body="Enable CloudCurrent VStrike and configure its URL and credentials in Integrations to view network context."
    primary={{ label: 'Configure VStrike', onClick: onConfigure, icon: 'gear' }} />
  const frameWidth = Math.max(1280, width)
  const scale = Math.min(1, width / frameWidth)
  const frameHeight = Math.max(720, 540 / scale)

  return <>
    <section className={`vstrike-panel${active ? '' : ' vstrike-parked'}${fullscreen ? ' vstrike-fullscreen' : ''}`} aria-hidden={!active}>
      {!enabled ? settings : phase === 'loading' ? <EmptyState loading icon="graph" title="Opening VStrike…" /> : phase === 'error' ? <EmptyState error icon="alert" title="VStrike is unavailable" body={error}
        primary={{ label: 'Reconnect', onClick: reconnect, icon: 'refresh' }} secondary={{ label: 'Configure VStrike', onClick: onConfigure, icon: 'gear' }} /> : <>
        <div className="vstrike-toolbar">{controls}
          <button className="btn ghost" disabled={!storyline || !networkLoaded || busy} onClick={() => void action(() => vstrikeApi.applyStoryline(network, storyline), 'apply the storyline')}>Apply</button>
          <button className="btn ghost icon" title="Previous storyline frame" disabled={!storyline || !networkLoaded || busy} onClick={() => void action(() => vstrikeApi.step('backward'), 'step backward')}><Icon name="chevL" /></button>
          <button className="btn ghost icon" title="Next storyline frame" disabled={!storyline || !networkLoaded || busy} onClick={() => void action(() => vstrikeApi.step('forward'), 'step forward')}><Icon name="chevR" /></button>
          <button className="btn ghost" onClick={props.onOpenEvents}>VStrike events</button>
          <button className="btn ghost" onClick={reconnect}><Icon name="refresh" size={14} />Reconnect</button>
          <button className="btn ghost" onClick={() => setFullscreen((value) => !value)}>{fullscreen ? 'Exit expanded view' : 'Expand graph'}</button>
        </div>
        {request && <div className="vstrike-selection" aria-label="Requested graph selection" role="region">
          <strong className="mono">{request.ips.join(' → ')}</strong>
          <p role="status">{selection === 'pending' ? 'Requesting selection…' : selection === 'requested' ? 'Selection requested · VStrike has not confirmed highlighting or zoom.' : selection === 'error' ? 'Selection could not be requested.' : !network ? 'Choose the network containing these endpoints.' : networkLoaded ? 'Use Retry selection to request focus.' : 'Waiting for the VStrike view.'}</p>
          <button className="btn ghost" disabled={!networkLoaded || !loaded || selection === 'pending' || busy} onClick={() => void select()}>Retry selection</button>
          {request.findingId && <button className="btn ghost" onClick={() => props.onBackToFinding(request.findingId!)}>Back to finding</button>}
          <button className="btn ghost" onClick={props.onBack}>Back to results</button>
        </div>}
        {error && <p role="alert" className="vstrike-error">{error}</p>}
        <p className="vstrike-note">External VStrike network context. If the graph shows a login screen, use Reconnect.</p>
        <div ref={surface} className="vstrike-frame" style={{ height: Math.max(540, frameHeight * scale) }}>
          {url && <iframe ref={iframe} title="CloudCurrent VStrike network visualization" src={url} referrerPolicy="no-referrer"
            onLoad={() => setLoaded(true)} style={{ width: frameWidth, height: frameHeight, transform: `scale(${scale})` }} />}
        </div>
      </>}
    </section>
    {eventsOpen && <FindingsDrawer title="VStrike events" onClose={onCloseEvents}>
      {!enabled ? settings : phase === 'loading' ? <p>Opening VStrike…</p> : phase === 'error' ? <p role="alert">{error}</p> : <>{controls}<VStrikeEvents storylineId={storyline} onFocus={props.onFocus} /></>}
    </FindingsDrawer>}
  </>
}
