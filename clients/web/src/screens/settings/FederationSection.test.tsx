import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import FederationSection from './FederationSection'
import type { FederationSourceView } from '../../services/api'

const state = vi.hoisted(() => ({
  phase: 'ready' as 'loading' | 'error' | 'ready',
  sources: [] as unknown[],
  globalEnabled: true,
  patchSource: vi.fn(() => Promise.resolve()),
  setGlobal: vi.fn(() => Promise.resolve()),
  pollNow: vi.fn(() => Promise.resolve()),
  reload: vi.fn(),
}))

vi.mock('./useSettings', () => ({
  useFederation: () => ({
    sources: state.sources,
    globalEnabled: state.globalEnabled,
    phase: state.phase,
    error: 'boom',
    reload: state.reload,
    setGlobal: state.setGlobal,
    patchSource: state.patchSource,
    pollNow: state.pollNow,
  }),
}))

const source = (over: Partial<FederationSourceView>): FederationSourceView => ({
  source_id: 'splunk',
  enabled: true,
  interval_seconds: 300,
  max_items: 100,
  min_severity: 'medium',
  cursor: { since: '2026-01-01T00:00:00Z' },
  last_poll_at: new Date(Date.now() - 60_000).toISOString(),
  last_success_at: new Date(Date.now() - 60_000).toISOString(),
  last_error: null,
  consecutive_errors: 0,
  lag_seconds: 60,
  quiet: false,
  is_configured: true,
  default_interval_seconds: 300,
  ...over,
})

const notify = vi.fn()
const row = (name: string) => screen.getByText(name).closest('tr') as HTMLElement

beforeEach(() => {
  vi.clearAllMocks()
  state.phase = 'ready'
  state.globalEnabled = true
  state.sources = [
    source({}),
    source({
      source_id: 'crowdstrike',
      lag_seconds: 900,
      quiet: true,
      consecutive_errors: 2,
      last_error: 'timeout',
      cursor: {},
      interval_seconds: 90,
    }),
  ]
})

describe('FederationSection', () => {
  it('shows loading and error states, with a retry', () => {
    state.phase = 'loading'
    const { rerender } = render(<FederationSection notify={notify} />)
    expect(screen.getByText(/Loading alert collection/)).toBeInTheDocument()
    state.phase = 'error'
    rerender(<FederationSection notify={notify} />)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(state.reload).toHaveBeenCalled()
  })

  it('explains an empty table', () => {
    state.sources = []
    render(<FederationSection notify={notify} />)
    expect(screen.getByText('No alert sources available')).toBeInTheDocument()
  })

  it('rates a quiet row Poor and a healthy row Good, never Fair', () => {
    render(<FederationSection notify={notify} />)
    expect(within(row('Splunk')).getByText('Good')).toBeInTheDocument()
    expect(within(row('CrowdStrike Falcon')).getByText('Poor')).toBeInTheDocument()
    expect(screen.queryByText('Fair')).toBeNull()
    expect(within(row('CrowdStrike Falcon')).getByTitle('timeout')).toHaveTextContent('2')
  })

  it('shows the cursor, lag and expected interval in the ⓘ', () => {
    render(<FederationSection notify={notify} />)
    fireEvent.click(within(row('Splunk')).getByRole('button', { name: /collection is measured/ }))
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent('"since":"2026-01-01T00:00:00Z"')
    expect(tip).toHaveTextContent('Lag is 1m')
    expect(tip).toHaveTextContent('Expected every 5 min')
    fireEvent.keyDown(tip, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('reads an empty cursor as none yet and a null lag as not measured', () => {
    state.sources = [source({ cursor: {}, lag_seconds: null, quiet: true, last_success_at: null })]
    render(<FederationSection notify={notify} />)
    fireEvent.click(screen.getByRole('button', { name: /collection is measured/ }))
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent('no cursor yet')
    expect(tip).toHaveTextContent('not measured yet')
  })

  it('does not round a sub-minute interval, and sends what was typed', () => {
    render(<FederationSection notify={notify} />)
    const input = screen.getByLabelText('Poll CrowdStrike Falcon every')
    expect(input).toHaveValue('90 s')
    fireEvent.change(input, { target: { value: '2' } })
    fireEvent.blur(input)
    expect(state.patchSource).toHaveBeenCalledWith('crowdstrike', { interval_seconds: 120 })
    fireEvent.change(input, { target: { value: '30s' } })
    fireEvent.blur(input)
    expect(state.patchSource).toHaveBeenLastCalledWith('crowdstrike', { interval_seconds: 30 })
  })

  it('rejects an interval outside 10s to 24h without a request', () => {
    render(<FederationSection notify={notify} />)
    const input = screen.getByLabelText('Poll Splunk every')
    fireEvent.change(input, { target: { value: '5s' } })
    fireEvent.blur(input)
    fireEvent.change(input, { target: { value: 'soon' } })
    fireEvent.blur(input)
    expect(state.patchSource).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledWith('err', expect.stringContaining('between 10 seconds and 24 hours'))
  })

  it('toggles a source, patches severity, and polls now', () => {
    render(<FederationSection notify={notify} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Collect from Splunk' }))
    expect(state.patchSource).toHaveBeenCalledWith('splunk', { enabled: false })
    fireEvent.click(within(row('Splunk')).getByRole('button', { name: 'Poll now' }))
    expect(state.pollNow).toHaveBeenCalledWith('splunk')
  })

  it('says the master switch is off and flips it', () => {
    state.globalEnabled = false
    render(<FederationSection notify={notify} />)
    expect(screen.getByText('Alert collection is off')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('switch', { name: 'Alert collection' }))
    expect(state.setGlobal).toHaveBeenCalledWith(true)
  })

  it('disables an unconfigured source', () => {
    state.sources = [source({ source_id: 'elastic', is_configured: false, enabled: false })]
    render(<FederationSection notify={notify} />)
    expect(screen.getByRole('switch', { name: 'Collect from Elastic Security' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Poll now' })).toBeDisabled()
  })
})
