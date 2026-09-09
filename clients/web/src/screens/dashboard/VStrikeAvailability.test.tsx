import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ExtensionProvider, useExtensions } from '../../extensions/ExtensionProvider'
import { mapApiFinding } from '../../data/mappers'
import { ToastProvider } from '../../shell/toast'
import DashboardScreen from './DashboardScreen'

const mocks = vi.hoisted(() => ({
  config: vi.fn(), finding: vi.fn(), connect: vi.fn(), networks: vi.fn(),
  storylines: vi.fn(), loadNetwork: vi.fn(), focus: vi.fn(), faults: vi.fn(),
}))
vi.mock('../../services/api', () => ({
  default: {},
  configApi: { getIntegrations: mocks.config },
  findingsApi: { getById: mocks.finding },
}))
vi.mock('../../integrations/vstrike/api', async (original) => ({
  ...await original<typeof import('../../integrations/vstrike/api')>(), vstrikeApi: mocks,
}))
const rawFinding = {
  finding_id: 'synthetic-availability', title: 'Example flow', severity: 'high',
  data_source: 'flow', timestamp: '2026-01-02T12:00:00Z', anomaly_score: 0.8,
  status: 'open', entity_context: { source_ip: '192.0.2.2', destination_ip: '198.51.100.10' },
}
const rows = [mapApiFinding(rawFinding)]
vi.mock('./useFindings', () => ({
  useFindings: () => ({ rows, phase: 'ready', reload: vi.fn() }),
  useDashboardKpis: () => ({ kpis: undefined, reload: vi.fn() }),
}))

function RefreshIntegrationSettings() {
  const { reload } = useExtensions()
  return <button onClick={reload}>Refresh integration settings</button>
}
function mount() {
  return render(<ExtensionProvider><MemoryRouter><ToastProvider>
    <RefreshIntegrationSettings />
    <DashboardScreen openChat={vi.fn()} go={vi.fn()} goSettings={vi.fn()} setViewFull={vi.fn()} />
  </ToastProvider></MemoryRouter></ExtensionProvider>)
}
function expectNoVStrike() {
  expect(screen.queryByRole('tab', { name: 'VStrike' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /VStrike/ })).not.toBeInTheDocument()
  expect(screen.queryByTitle('CloudCurrent VStrike network visualization')).not.toBeInTheDocument()
}
function settingsUnavailable(state: string) {
  if (state === 'loading') mocks.config.mockImplementation(() => new Promise(() => {}))
  else if (state === 'failed') mocks.config.mockRejectedValue(new Error('Configuration unavailable'))
  else mocks.config.mockResolvedValue({ data: { enabled_integrations: ['unrelated-integration'] } })
}

beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear()
  mocks.config.mockResolvedValue({ data: { enabled_integrations: ['vstrike'] } })
  mocks.finding.mockResolvedValue({ data: rawFinding })
  mocks.connect.mockResolvedValue({ data: { iframe_url: 'https://vstrike.example.test/login?token=synthetic' } })
  mocks.networks.mockResolvedValue({ data: { networks: [{ id: 'network-1', name: 'Example network' }] } })
  mocks.storylines.mockResolvedValue({ data: { storylines: [{ id: 'storyline-1', name: 'Example scenario' }] } })
  mocks.loadNetwork.mockResolvedValue({}); mocks.focus.mockResolvedValue({})
  mocks.faults.mockResolvedValue({ data: { faults: [], fetched_at: '2026-01-02T12:00:00Z', truncated: false } })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.setAttribute('open', '') } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.removeAttribute('open') } })
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('VStrike integration availability', () => {
  it.each(['disabled', 'loading', 'failed'])('hides every entry point and sends no provider requests when settings are %s', async (state) => {
    settingsUnavailable(state)
    mount()
    await act(async () => {})
    expectNoVStrike()
    fireEvent.click(screen.getByRole('button', { name: 'Example flow' }))
    const detail = await screen.findByRole('dialog')
    await within(detail).findByRole('heading', { name: 'Example flow' })
    expectNoVStrike()
    expect(mocks.connect).not.toHaveBeenCalled()
    expect(mocks.networks).not.toHaveBeenCalled()
    expect(mocks.faults).not.toHaveBeenCalled()
  })

  it('restores entry points when enabled and opens the graph from real finding details', async () => {
    settingsUnavailable('disabled')
    mount()
    await act(async () => {})
    expectNoVStrike()
    mocks.config.mockResolvedValue({ data: { enabled_integrations: ['vstrike'] } })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh integration settings' }))
    expect(await screen.findByRole('tab', { name: 'VStrike' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'VStrike events' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'View synthetic-availability endpoints in VStrike' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Example flow' }))
    const detail = await screen.findByRole('dialog')
    fireEvent.click(await within(detail).findByRole('button', { name: /endpoints in VStrike/ }))
    expect(await screen.findByTitle('CloudCurrent VStrike network visualization')).toBeVisible()
    expect(screen.getByRole('tab', { name: 'VStrike' })).toHaveAttribute('aria-selected', 'true')
    expect(mocks.connect).toHaveBeenCalledTimes(1)
  })

  it.each(['disabled', 'loading', 'failed'])('closes active graph/events, ignores a late read, and preserves the queue when settings become %s', async (state) => {
    mount()
    await screen.findByRole('tab', { name: 'VStrike' })
    fireEvent.change(screen.getByRole('textbox', { name: 'Search findings' }), { target: { value: '192.0.2.2' } })
    fireEvent.click(screen.getByRole('button', { name: /endpoints in VStrike/ }))
    const frame = await screen.findByTitle('CloudCurrent VStrike network visualization') as HTMLIFrameElement
    fireEvent.load(frame)
    fireEvent(window, new MessageEvent('message', { origin: 'https://vstrike.example.test', source: frame.contentWindow, data: { type: 'vstrike:state', networkId: 'network-1' } }))
    await waitFor(() => expect(mocks.focus).toHaveBeenCalledTimes(1))
    let finishRead: (value: unknown) => void = () => {}
    mocks.faults.mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve }))
    fireEvent.click(screen.getByRole('button', { name: 'VStrike events' }))
    await screen.findByRole('dialog', { name: 'VStrike events' })
    expect(mocks.faults).toHaveBeenCalledTimes(1)
    settingsUnavailable(state)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh integration settings' }))
    await act(async () => {})
    expectNoVStrike()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Findings' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('textbox', { name: 'Search findings' })).toHaveValue('192.0.2.2')
    vi.useFakeTimers()
    await act(async () => { finishRead({ data: { faults: [], fetched_at: '2026-01-02T12:00:00Z', truncated: false } }) })
    await act(async () => { vi.advanceTimersByTime(20_000) })
    expect(mocks.faults).toHaveBeenCalledTimes(1)
    expect(mocks.connect).toHaveBeenCalledTimes(1)
    expect(mocks.focus).toHaveBeenCalledTimes(1)
    expect(mocks.loadNetwork).not.toHaveBeenCalled()
  })
})
