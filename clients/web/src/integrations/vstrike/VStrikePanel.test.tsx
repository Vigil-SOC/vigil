import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import VStrikePanel, { type GraphRequest } from './VStrikePanel'
import VStrikeEvents from './VStrikeEvents'
import { options } from './api'

const mocks = vi.hoisted(() => ({ enabled: true, connect: vi.fn(), networks: vi.fn(), storylines: vi.fn(), loadNetwork: vi.fn(), focus: vi.fn(), faults: vi.fn(), applyStoryline: vi.fn(), step: vi.fn() }))
vi.mock('../../extensions/ExtensionProvider', () => ({ useExtensions: () => ({ enabledIntegrations: mocks.enabled ? ['vstrike'] : [], loading: false }) }))
vi.mock('./api', async (original) => ({ ...await original<typeof import('./api')>(), vstrikeApi: mocks }))

const request: GraphRequest = { id: 1, ips: ['192.0.2.2', '198.51.100.10'], findingId: 'synthetic-1' }
const baseProps = { active: true, request, eventsOpen: false, onCloseEvents: vi.fn(), onOpenEvents: vi.fn(), onFocus: vi.fn(), onBack: vi.fn(), onBackToFinding: vi.fn(), onConfigure: vi.fn() }

beforeEach(() => {
  vi.clearAllMocks(); mocks.enabled = true
  mocks.connect.mockResolvedValue({ data: { iframe_url: 'https://vstrike.example.test/login?token=synthetic' } })
  mocks.networks.mockResolvedValue({ data: { networks: [{ networkId: 'network-1', name: 'Example network' }] } })
  mocks.storylines.mockResolvedValue({ data: { storylines: [{ storylineSetId: 'storyline-1', name: 'Example scenario' }] } })
  mocks.loadNetwork.mockResolvedValue({}); mocks.focus.mockResolvedValue({}); mocks.applyStoryline.mockResolvedValue({})
  mocks.faults.mockResolvedValue({ data: { faults: [], fetched_at: '2026-01-02T12:00:00Z', truncated: false, limit: 100 } })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.setAttribute('open', '') } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.removeAttribute('open') } })
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

async function ready() {
  const frame = await screen.findByTitle('CloudCurrent VStrike network visualization')
  fireEvent.load(frame)
  // Network acknowledgement must come from this exact iframe, not another tab.
  fireEvent(window, new MessageEvent('message', { origin: 'https://vstrike.example.test', source: (frame as HTMLIFrameElement).contentWindow, data: { type: 'vstrike:state', networkId: 'network-1' } }))
  return frame
}

describe('VStrike network workflow', () => {
  it('keeps disabled integrations idle and directs configuration', () => {
    mocks.enabled = false
    render(<VStrikePanel {...baseProps} />)
    fireEvent.click(screen.getByRole('button', { name: 'Configure VStrike' }))
    expect(baseProps.onConfigure).toHaveBeenCalled()
    expect(mocks.connect).not.toHaveBeenCalled()
  })
  it('uses the configured network and exposes selection as unconfirmed', async () => {
    render(<VStrikePanel {...baseProps} />)
    await ready()
    await waitFor(() => expect(mocks.focus).toHaveBeenCalledWith('network-1', request.ips))
    expect(await screen.findByText(/has not confirmed highlighting or zoom/)).toBeVisible()
    expect(screen.getByLabelText('VStrike storyline')).toHaveValue('storyline-1')
    fireEvent.click(screen.getByRole('button', { name: 'Back to finding' }))
    expect(baseProps.onBackToFinding).toHaveBeenCalledWith('synthetic-1')
  })
  it('requires a choice when more than one network is available', async () => {
    mocks.networks.mockResolvedValue({ data: { networks: [{ id: 'network-1', name: 'One' }, { id: 'network-2', name: 'Two' }] } })
    render(<VStrikePanel {...baseProps} />)
    await screen.findByTitle('CloudCurrent VStrike network visualization')
    expect(screen.getByLabelText('VStrike network')).toHaveValue('')
    expect(mocks.focus).not.toHaveBeenCalled()
  })
  it('preserves the iframe across tab changes and does not apply a late selection result', async () => {
    let resolve: (value: unknown) => void = () => {}
    mocks.focus.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const view = render(<VStrikePanel {...baseProps} />)
    const frame = await ready()
    await waitFor(() => expect(mocks.focus).toHaveBeenCalled())
    view.rerender(<VStrikePanel {...baseProps} active={false} />)
    await act(async () => resolve({}))
    view.rerender(<VStrikePanel {...baseProps} />)
    expect(screen.getByTitle('CloudCurrent VStrike network visualization')).toBe(frame)
    expect(screen.queryByText(/has not confirmed highlighting or zoom/)).not.toBeInTheDocument()
    expect(mocks.connect).toHaveBeenCalledTimes(1)
  })
  it('bounds network retries and reapplies the current selection after a retry', async () => {
    vi.useFakeTimers()
    render(<VStrikePanel {...baseProps} />)
    await act(async () => {})
    fireEvent.load(screen.getByTitle('CloudCurrent VStrike network visualization'))
    await act(async () => { vi.advanceTimersByTime(1_000) })
    expect(mocks.loadNetwork).toHaveBeenCalledTimes(1)
    expect(mocks.focus).toHaveBeenCalledTimes(1)
    await act(async () => { vi.advanceTimersByTime(11_000) })
    expect(mocks.loadNetwork).toHaveBeenCalledTimes(2)
    expect(mocks.focus).toHaveBeenCalledTimes(2)
    await act(async () => { vi.advanceTimersByTime(60_000) })
    expect(mocks.loadNetwork).toHaveBeenCalledTimes(2)
    expect(mocks.focus).toHaveBeenLastCalledWith('network-1', request.ips)
  })
  it('waits for the replacement iframe before selecting after reconnect', async () => {
    render(<VStrikePanel {...baseProps} />)
    await ready()
    await waitFor(() => expect(mocks.focus).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }))
    await screen.findByTitle('CloudCurrent VStrike network visualization')
    expect(mocks.focus).toHaveBeenCalledTimes(1)
    await ready()
    await waitFor(() => expect(mocks.focus).toHaveBeenCalledTimes(2))
  })
  it('ignores messages with the wrong origin or window and cancels retries only for the matching iframe', async () => {
    vi.useFakeTimers()
    render(<VStrikePanel {...baseProps} />)
    await act(async () => {})
    const frame = screen.getByTitle('CloudCurrent VStrike network visualization') as HTMLIFrameElement
    fireEvent.load(frame)
    const data = { type: 'vstrike:state', networkId: 'network-1' }
    fireEvent(window, new MessageEvent('message', { origin: 'https://vstrike.example.test', source: window, data }))
    fireEvent(window, new MessageEvent('message', { origin: 'https://untrusted.example.test', source: frame.contentWindow, data }))
    await act(async () => {})
    expect(mocks.focus).not.toHaveBeenCalled()
    fireEvent(window, new MessageEvent('message', { origin: 'https://vstrike.example.test', source: frame.contentWindow, data }))
    await act(async () => { vi.advanceTimersByTime(60_000) })
    expect(mocks.focus).toHaveBeenCalledTimes(1)
    expect(mocks.loadNetwork).not.toHaveBeenCalled()
  })
  it('does not expose authenticated URLs in provider errors', async () => {
    mocks.connect.mockRejectedValue(new Error('https://vstrike.example.test?token=do-not-display'))
    render(<VStrikePanel {...baseProps} />)
    expect(await screen.findByText('VStrike is unavailable')).toBeVisible()
    expect(screen.queryByText(/do-not-display/)).not.toBeInTheDocument()
  })
  it('normalizes options without choosing a named deployment', () => {
    expect(options([{ networkId: 'one', name: 'Any network' }, { networkId: 'one' }, null], ['networkId'])).toEqual([{ id: 'one', label: 'Any network' }])
  })
})

describe('external scenario drawer', () => {
  it('reads only while open, reports truncation and keeps measurement zero', async () => {
    mocks.faults.mockResolvedValue({ data: { faults: [{ event_id: 'event-1', label: 'Synthetic event', description: 'Example only', occurred_at: '2026-01-02T12:00:00Z', source_level: 'danger', ip4s: ['192.0.2.2'], measurement: { value: '0' } }], fetched_at: '2026-01-02T12:00:01Z', truncated: true, limit: 100 } })
    const view = render(<VStrikePanel {...baseProps} />)
    await ready()
    expect(mocks.faults).not.toHaveBeenCalled()
    view.rerender(<VStrikePanel {...baseProps} eventsOpen />)
    const drawer = await screen.findByRole('dialog', { name: 'VStrike events' })
    expect(await within(drawer).findByText('Synthetic event')).toBeVisible()
    expect(within(drawer).getByText(/latest page only/)).toBeVisible()
    fireEvent.click(within(drawer).getByText('Measurement details'))
    expect(within(drawer).getByText('0')).toBeInTheDocument()
    fireEvent.click(within(drawer).getByRole('button', { name: 'Open in graph' }))
    expect(baseProps.onFocus).toHaveBeenCalledWith(['192.0.2.2'])
    view.unmount()
    vi.useFakeTimers(); await act(async () => { vi.advanceTimersByTime(10_000) })
    expect(mocks.faults).toHaveBeenCalledTimes(1)
  })
  it('retains the last page on a failed refresh without inventing an empty success', async () => {
    vi.useFakeTimers()
    const view = render(<VStrikeEvents storylineId="storyline-1" onFocus={vi.fn()} />)
    await act(async () => {})
    mocks.faults.mockRejectedValueOnce(new Error('offline'))
    await act(async () => { vi.advanceTimersByTime(5_000) })
    expect(screen.getByRole('alert')).toHaveTextContent('Showing the last successful read')
    view.unmount()
    await act(async () => { vi.advanceTimersByTime(10_000) })
    expect(mocks.faults).toHaveBeenCalledTimes(2)
  })
})
