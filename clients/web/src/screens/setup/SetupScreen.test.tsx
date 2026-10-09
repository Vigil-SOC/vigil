import { StrictMode, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import SetupScreen from './SetupScreen'
import { SETUP_DISMISSED_KEY, SETUP_PROGRESS_KEY } from './setupDismissed'
import { configApi, federationApi, mcpApi } from '../../services/api'
import { TEST_POLL_MS, TEST_POLL_TRIES } from './SourceCollection'

const auth = vi.hoisted(() => ({
  allowed: true,
}))

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ hasPermission: () => auth.allowed }),
}))

vi.mock('../../contexts/ColorSchemeContext', () => ({
  useColorScheme: () => ({ scheme: 'dark', setScheme: vi.fn(), toggleScheme: vi.fn() }),
}))

// stands in for the real form: Save runs the dialog's save, as the wizard does
vi.mock('../settings/IntegrationWizard', () => ({
  default: function WizardStub({
    integration,
    onSave,
    onClose,
  }: {
    integration: { id: string }
    onSave: (id: string, config: Record<string, unknown>) => Promise<void>
    onClose: () => void
  }) {
    const [err, setErr] = useState('')
    return (
      <div>
        {err && <p>{err}</p>}
        <button
          onClick={() =>
            onSave(integration.id, {}).then(onClose, (e: Error) => setErr(e.message))
          }
        >
          Save source
        </button>
      </div>
    )
  },
}))

vi.mock('../../services/api', () => ({
  consoleApi: { getHealth: vi.fn(() => Promise.resolve({ data: { status: 'healthy' } })) },
  storageApi: { getStatus: vi.fn(() => Promise.resolve({ data: { backend: 'none' } })) },
  llmProviderApi: { list: vi.fn(() => Promise.resolve({ data: [] })) },
  aiConfigApi: { getConfig: vi.fn(() => Promise.resolve({ data: { components: [], assignments: {} } })) },
  workflowApi: { listAll: vi.fn(() => Promise.resolve({ data: { workflows: [] } })) },
  configApi: {
    getOrchestrator: vi.fn(() => Promise.resolve({ data: { profiles: {} } })),
    setOrchestrator: vi.fn(),
    getForceManualApproval: vi.fn(() =>
      Promise.resolve({ data: { enabled: false, environment_wins: false } }),
    ),
    setForceManualApproval: vi.fn(),
    getIntegrations: vi.fn(() => Promise.resolve({ data: {} })),
    getAutonomy: vi.fn(() =>
      Promise.resolve({ data: { auto_response_enabled: false, force_manual_approval: true } }),
    ),
    setIntegrations: vi.fn(() => Promise.resolve({ data: {} })),
    getDemoMode: vi.fn(() => Promise.resolve({ data: { enabled: false, source: 'file' } })),
    setDemoMode: vi.fn(() => Promise.resolve({ data: { enabled: true } })),
  },
  mcpApi: {
    listServers: vi.fn(() => Promise.resolve({ data: { servers: [] } })),
    setServerEnabled: vi.fn(() => Promise.resolve({ data: { connected: true } })),
    getStatuses: vi.fn(() => Promise.resolve({ data: { statuses: [] } })),
  },
  federationApi: {
    getHealth: vi.fn(() => Promise.resolve({ data: { sources: [] } })),
    listSources: vi.fn(),
    updateSource: vi.fn(),
    pollNow: vi.fn(() => Promise.resolve({ data: { ok: true } })),
    setSettings: vi.fn(),
  },
}))

vi.mock('../../services/bifrostApi', () => ({
  anyRoutableBifrostProvider: vi.fn(() => Promise.resolve(false)),
  COMMON_PROVIDERS: ['ollama', 'openai'],
  keyRefusal: vi.fn(),
  secretText: () => '',
  bifrostApi: {
    listProviders: vi.fn(() => Promise.resolve({ data: { providers: [] } })),
    listKeys: vi.fn(),
    routability: vi.fn(() => Promise.resolve({ data: { providers: {} } })),
    createProvider: vi.fn(),
    createKey: vi.fn(),
  },
}))

function renderSetup() {
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={['/setup']}>
        <Routes>
          <Route path="/setup" element={<SetupScreen />} />
          <Route path="/" element={<div>console-home</div>} />
        </Routes>
      </MemoryRouter>
    </StrictMode>,
  )
}

const source = (over: Record<string, unknown> = {}) => ({
  source_id: 'crowdstrike',
  enabled: true,
  interval_seconds: 300,
  min_severity: null,
  last_poll_at: '2026-10-06T10:00:00Z',
  last_success_at: null,
  last_error: null,
  consecutive_errors: 0,
  is_configured: true,
  ...over,
})
const listing = (sources: unknown[], enabled = true) =>
  ({ data: { sources, global: { enabled } } }) as never

describe('SetupScreen', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    auth.allowed = true
  })

  it('themes the page root from the scheme', () => {
    const { container } = renderSetup()
    expect(container.querySelector('.soc-console')).toHaveClass('vg-dark')
  })

  const saved = () => JSON.parse(localStorage.getItem(SETUP_PROGRESS_KEY) ?? 'null')
  const rail = () => within(screen.getByRole('complementary', { name: 'Setup steps' }))

  it('starts at step 1 with no Back and no per-step Skip', () => {
    renderSetup()
    expect(screen.getByText('Step 1 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Skip$/ })).not.toBeInTheDocument()
    expect(screen.getByText('Step 1 of 5 · your progress is saved')).toBeInTheDocument()
  })

  it('Continue never disables, passes the step and shows Back', async () => {
    renderSetup()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Step 2 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
    expect(saved()).toEqual({ furthest: 2, passed: [1] })
    expect(rail().getByRole('button', { name: /Before you start/ })).toHaveTextContent('Before you start')
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByText('Step 1 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
  })

  it('reads Finish setup on step 5 and the done page is not counted', () => {
    localStorage.setItem(SETUP_PROGRESS_KEY, JSON.stringify({ furthest: 5, passed: [1, 2, 3, 4] }))
    renderSetup()
    expect(screen.getByText('Step 5 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
    expect(screen.getByText('Setup complete', { selector: '.su-eyebrow' })).toBeInTheDocument()
    expect(screen.queryByText(/of 5/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Go to console/ })).toBeInTheDocument()
    expect(rail().getAllByRole('button').filter((b) => b.querySelector('svg'))).toHaveLength(5)
    expect(saved().passed).toContain(5)
  })

  it('ticks the rail from stored progress and opens any item', async () => {
    localStorage.setItem(SETUP_PROGRESS_KEY, JSON.stringify({ furthest: 3, passed: [1, 2] }))
    renderSetup()
    expect(screen.getByText('Step 3 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
    const items = rail().getAllByRole('button').slice(0, 5)
    expect(items.map((b) => !!b.querySelector('svg'))).toEqual([true, true, false, false, false])
    expect(items[2]).toHaveAttribute('aria-current', 'step')
    fireEvent.click(items[4])
    expect(screen.getByText('Step 5 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
  })

  it('Save and finish later keeps progress, dismisses and leaves', () => {
    localStorage.setItem(SETUP_PROGRESS_KEY, JSON.stringify({ furthest: 2, passed: [1] }))
    renderSetup()
    fireEvent.click(screen.getByRole('button', { name: 'Save and finish later' }))
    expect(screen.getByText('console-home')).toBeInTheDocument()
    expect(localStorage.getItem(SETUP_DISMISSED_KEY)).toBe('1')
    expect(saved()).toEqual({ furthest: 2, passed: [1] })
  })

  it('reopening resumes at the saved step', () => {
    renderSetup()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    cleanup()
    renderSetup()
    expect(screen.getByText('Step 2 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
  })

  describe('stored progress that cannot be used', () => {
    it.each([
      ['malformed', '{not json'],
      ['the wrong type', '"3"'],
      ['an old format with a step id', JSON.stringify({ step: 'workflows' })],
      ['a step id that no longer exists', JSON.stringify({ furthest: 'summary', passed: ['checks'] })],
      ['a step outside 1-5', JSON.stringify({ furthest: 6, passed: [1] })],
      ['a passed step outside 1-5', JSON.stringify({ furthest: 2, passed: [0] })],
    ])('%s opens at step 1 with nothing passed', (_name, raw) => {
      localStorage.setItem(SETUP_PROGRESS_KEY, raw)
      renderSetup()
      expect(screen.getByText('Step 1 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
      expect(rail().getAllByRole('button').slice(0, 5).some((b) => b.querySelector('svg'))).toBe(false)
    })

    it('a localStorage that throws still renders step 1 and still moves on', async () => {
      const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
        throw new Error('denied')
      })
      const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
        throw new Error('denied')
      })
      renderSetup()
      expect(screen.getByText('Step 1 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      expect(await screen.findByText('Step 2 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
      get.mockRestore()
      set.mockRestore()
    })
  })

  describe('Skip setup and look around with demo data', () => {
    const link = () =>
      screen.getByRole('button', { name: 'Skip setup and look around with demo data' })

    it('turns demo mode on, dismisses and leaves', async () => {
      renderSetup()
      fireEvent.click(link())
      expect(await screen.findByText('console-home')).toBeInTheDocument()
      expect(configApi.setDemoMode).toHaveBeenCalledWith(true)
      expect(localStorage.getItem(SETUP_DISMISSED_KEY)).toBe('1')
    })

    it('stays on the page with a visible error when demo data cannot be turned on', async () => {
      vi.mocked(configApi.setDemoMode).mockRejectedValueOnce(new Error('500'))
      renderSetup()
      fireEvent.click(link())
      expect(await screen.findByRole('alert')).toHaveTextContent('Demo data could not be turned on')
      expect(screen.queryByText('console-home')).not.toBeInTheDocument()
      expect(localStorage.getItem(SETUP_DISMISSED_KEY)).toBeNull()
    })

    it('does not override a server-set DEMO_MODE=false', async () => {
      vi.mocked(configApi.getDemoMode).mockResolvedValueOnce({
        data: { enabled: false, source: 'environment' },
      } as never)
      renderSetup()
      fireEvent.click(link())
      expect(await screen.findByRole('alert')).toHaveTextContent(
        "Demo mode is set by the server's environment",
      )
      await waitFor(() => expect(link()).toBeEnabled())
      expect(configApi.setDemoMode).not.toHaveBeenCalled()
      expect(localStorage.getItem(SETUP_DISMISSED_KEY)).toBeNull()
    })
  })

  it('lets someone who cannot write settings leave for the console', () => {
    auth.allowed = false
    renderSetup()
    fireEvent.click(screen.getByRole('button', { name: /Continue to console/ }))
    expect(screen.getByText('console-home')).toBeInTheDocument()
    expect(localStorage.getItem(SETUP_DISMISSED_KEY)).toBe('1')
  })

  it('opens the summary on the done page and jumps back to a step from Change', async () => {
    renderSetup()
    for (let i = 0; i < 4; i++) fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
    expect(await screen.findByText('Nothing connected yet')).toBeInTheDocument()
    expect(screen.getByText('No provider')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Go to console/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Change Workflows' }))
    expect(await screen.findByText('No workflows yet.')).toBeInTheDocument()
    expect(screen.getByText('Step 4 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Change Limits' }))
    expect(await screen.findByRole('button', { name: /Assist/ })).toBeInTheDocument()
    expect(screen.getByText('Step 5 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
  })

  describe('Connect data after a save', () => {
    const connect = async () => {
      vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: ['crowdstrike'] } } as never)
      renderSetup()
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      fireEvent.click(await screen.findByText('CrowdStrike Falcon'))
      fireEvent.click(screen.getByRole('button', { name: 'Save source' }))
      await screen.findByText('Connected to CrowdStrike Falcon')
    }

    beforeEach(() => {
      vi.clearAllMocks()
      vi.mocked(federationApi.listSources).mockResolvedValue(listing([source()]))
    })
    afterEach(() => {
      vi.useRealTimers()
    })

    it('stays on the step with a Connected row, the source row and Connect another', async () => {
      await connect()
      expect(await screen.findByRole('switch', { name: 'Collect alerts' })).toBeChecked()
      expect(screen.getByRole('button', { name: 'Test' })).toBeEnabled()
      expect(screen.getByRole('button', { name: 'Continue' })).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Connect another' }))
      expect(screen.getByPlaceholderText(/Search data sources/)).toBeInTheDocument()
    })

    it('keeps the error when the connection fails', async () => {
      vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: ['crowdstrike'] } } as never)
      vi.mocked(mcpApi.setServerEnabled).mockResolvedValue({
        data: { connected: false, error: 'Bad credentials' },
      } as never)
      renderSetup()
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      fireEvent.click(await screen.findByText('CrowdStrike Falcon'))
      fireEvent.click(screen.getByRole('button', { name: 'Save source' }))
      expect(await screen.findByText('Bad credentials')).toBeInTheDocument()
      expect(screen.queryByText(/Connected to/)).not.toBeInTheDocument()
      vi.mocked(mcpApi.setServerEnabled).mockResolvedValue({ data: { connected: true } } as never)
    })

    it('saves the collection switch through updateSource', async () => {
      vi.mocked(federationApi.updateSource).mockResolvedValue({
        data: source({ enabled: false }),
      } as never)
      await connect()
      // the hook re-reads the list after a save; it must report the switch as off
      vi.mocked(federationApi.listSources).mockResolvedValue(listing([source({ enabled: false })]))
      fireEvent.click(await screen.findByRole('switch', { name: 'Collect alerts' }))
      expect(federationApi.updateSource).toHaveBeenCalledWith('crowdstrike', { enabled: false })
      expect(await screen.findByRole('button', { name: 'Test' })).toBeDisabled()
      expect(screen.getByText('Turn on Collect alerts to test.')).toBeInTheDocument()
    })

    it('saves a typed interval on blur, and ignores a blank one', async () => {
      vi.mocked(federationApi.updateSource).mockResolvedValue({
        data: source({ interval_seconds: 600 }),
      } as never)
      await connect()
      const input = await screen.findByLabelText('Interval (s)')
      fireEvent.change(input, { target: { value: '' } })
      fireEvent.blur(input)
      expect(federationApi.updateSource).not.toHaveBeenCalled()
      fireEvent.change(input, { target: { value: '600' } })
      fireEvent.blur(input)
      expect(federationApi.updateSource).toHaveBeenCalledWith('crowdstrike', { interval_seconds: 600 })
    })

    it('says collection is off globally and turns it on', async () => {
      vi.mocked(federationApi.listSources).mockResolvedValue(listing([source()], false))
      vi.mocked(federationApi.setSettings).mockResolvedValue({ data: { enabled: true } } as never)
      await connect()
      expect(await screen.findByText(/Alert collection is off/)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Test' })).toBeDisabled()
      fireEvent.click(screen.getByRole('switch', { name: 'Alert collection' }))
      expect(federationApi.setSettings).toHaveBeenCalledWith(true)
    })

    it('Test queues a poll and reports last_success_at once the poll advanced', async () => {
      await connect()
      await screen.findByRole('switch', { name: 'Collect alerts' })
      vi.useFakeTimers()
      vi.mocked(federationApi.listSources)
        .mockResolvedValueOnce(listing([source()])) // baseline read before queueing
        .mockResolvedValueOnce(listing([source()])) // stale: poll has not run yet
        .mockResolvedValue(
          listing([
            source({ last_poll_at: '2026-10-06T10:05:00Z', last_success_at: '2026-10-06T10:05:00Z' }),
          ]),
        )
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Test' }))
      })
      expect(federationApi.pollNow).toHaveBeenCalledWith('crowdstrike')
      await act(() => vi.advanceTimersByTimeAsync(TEST_POLL_MS))
      expect(screen.queryByText(/Last success/)).not.toBeInTheDocument()
      await act(() => vi.advanceTimersByTimeAsync(TEST_POLL_MS))
      expect(screen.getByText(/Last success/)).toBeInTheDocument()
    })

    it('Test shows last_error', async () => {
      await connect()
      await screen.findByRole('switch', { name: 'Collect alerts' })
      vi.useFakeTimers()
      vi.mocked(federationApi.listSources)
        .mockResolvedValueOnce(listing([source()])) // baseline read before queueing
        .mockResolvedValue(
          listing([source({ last_poll_at: '2026-10-06T10:05:00Z', last_error: '401 Unauthorized' })]),
        )
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Test' }))
      })
      await act(() => vi.advanceTimersByTimeAsync(TEST_POLL_MS))
      expect(screen.getByText('401 Unauthorized')).toBeInTheDocument()
    })

    it('Test gives up with a queued line when the poll never advances', async () => {
      await connect()
      await screen.findByRole('switch', { name: 'Collect alerts' })
      vi.useFakeTimers()
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Test' }))
      })
      await act(() => vi.advanceTimersByTimeAsync(TEST_POLL_MS * TEST_POLL_TRIES))
      expect(screen.getByText(/Queued · the collector runs it/)).toBeInTheDocument()
      const calls = vi.mocked(federationApi.listSources).mock.calls.length
      await act(() => vi.advanceTimersByTimeAsync(TEST_POLL_MS * 5))
      expect(vi.mocked(federationApi.listSources).mock.calls.length).toBe(calls)
    })

    it('shows the load error with a retry', async () => {
      vi.mocked(federationApi.listSources).mockRejectedValue(new Error('boom'))
      await connect()
      expect(await screen.findByText(/Couldn.t load collection settings: boom/)).toBeInTheDocument()
    })

    it('shows only the Connected row for an integration with no federation source', async () => {
      vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: ['opensearch'] } } as never)
      vi.mocked(federationApi.listSources).mockClear()
      renderSetup()
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      fireEvent.click(await screen.findByText(/OpenSearch/))
      fireEvent.click(screen.getByRole('button', { name: 'Save source' }))
      expect(await screen.findByText(/^Connected to /)).toBeInTheDocument()
      expect(screen.queryByRole('switch')).not.toBeInTheDocument()
      expect(federationApi.listSources).not.toHaveBeenCalled()
    })

    it('a second save merges with the first, not the config read at mount', async () => {
      vi.mocked(mcpApi.listServers).mockResolvedValue({
        data: { servers: ['crowdstrike', 'opensearch'] },
      } as never)
      vi.mocked(configApi.setIntegrations).mockClear()
      renderSetup()
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      fireEvent.click(await screen.findByText('CrowdStrike Falcon'))
      fireEvent.click(screen.getByRole('button', { name: 'Save source' }))
      await screen.findByText('Connected to CrowdStrike Falcon')
      fireEvent.click(screen.getByRole('button', { name: 'Connect another' }))
      fireEvent.click(await screen.findByText(/OpenSearch/))
      fireEvent.click(screen.getByRole('button', { name: 'Save source' }))
      await screen.findByText(/^Connected to OpenSearch/)
      const calls = vi.mocked(configApi.setIntegrations).mock.calls
      const second = calls[calls.length - 1][0] as { enabled_integrations: string[] }
      expect(second.enabled_integrations).toEqual(
        expect.arrayContaining(['crowdstrike', 'opensearch']),
      )
    })
  })
})
