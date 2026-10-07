import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import SetupScreen from './SetupScreen'
import { SETUP_DISMISSED_KEY, SETUP_PROGRESS_KEY } from './setupDismissed'
import { configApi, federationApi, ingestionApi, mcpApi } from '../../services/api'
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
  ingestionApi: {
    listJobs: vi.fn(() => Promise.resolve({ data: [] })),
    getJob: vi.fn(),
    uploadFile: vi.fn(),
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

    expect(screen.getByRole('button', { name: /Assist/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Change On their own' }))
    expect(await screen.findByText('No workflows yet.')).toBeInTheDocument()
    expect(screen.getByText('Step 4 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Change Limits' }))
    expect(await screen.findByRole('radiogroup', { name: 'Limits profile' })).toBeInTheDocument()
    expect(screen.getByText('Step 5 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
  })

  describe('Connect your data', () => {
    const SERVERS = [
      'crowdstrike',
      'sentinelone',
      'carbon-black',
      'microsoft-defender',
      'loglm',
      'splunk-selfhosted',
      'elastic',
      'opensearch',
    ]
    const serve = (servers: string[]) =>
      vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers } } as never)
    const openStep = async (servers = ['crowdstrike']) => {
      serve(servers)
      renderSetup()
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      await screen.findByRole('group', { name: 'Data source' })
      await screen.findByRole('button', { name: 'Test connection' })
    }
    const fillCrowdStrike = () => {
      fireEvent.change(screen.getByLabelText('Client ID'), { target: { value: 'id-1' } })
      fireEvent.change(screen.getByLabelText('Client Secret'), { target: { value: 'secret-1' } })
    }
    const testConnection = () => fireEvent.click(screen.getByRole('button', { name: /^Test (connection|again)$/ }))
    const connect = async () => {
      await openStep()
      fillCrowdStrike()
      testConnection()
      await screen.findByText('Connected to CrowdStrike Falcon')
    }
    const failWith = (data: Record<string, unknown>) =>
      vi.mocked(mcpApi.setServerEnabled).mockResolvedValue({ data: { connected: false, ...data } } as never)

    beforeEach(() => {
      vi.clearAllMocks()
      vi.mocked(mcpApi.setServerEnabled).mockResolvedValue({ data: { connected: true } } as never)
      vi.mocked(configApi.getIntegrations).mockResolvedValue({ data: {} } as never)
      vi.mocked(federationApi.listSources).mockResolvedValue(listing([source()]))
    })
    afterEach(() => {
      vi.useRealTimers()
    })

    describe('the tiles', () => {
      const tiles = () => within(screen.getByRole('group', { name: 'Data source' })).getAllByRole('button')

      it('follow the filter in catalog order, then Upload a file and Try demo data', async () => {
        await openStep(SERVERS)
        expect(tiles().map((t) => t.querySelector('.su-choice-t')?.textContent)).toEqual([
          'CrowdStrike Falcon',
          'SentinelOne',
          'VMware Carbon Black',
          'Microsoft Defender for Endpoint',
          'LogLM',
          'Splunk',
          'Upload a file',
          'Try demo data',
        ])
        expect(screen.getByText('DeepTempo')).toBeInTheDocument()
        expect(screen.getByText('No setup')).toBeInTheDocument()
        expect(tiles()[0]).toHaveAttribute('aria-pressed', 'true')
      })

      it('leave LogLM out when its server is absent', async () => {
        await openStep(['crowdstrike', 'splunk-selfhosted'])
        expect(screen.queryByText('LogLM')).not.toBeInTheDocument()
        expect(screen.queryByText('DeepTempo')).not.toBeInTheDocument()
        expect(screen.queryByRole('button', { name: 'More sources' })).not.toBeInTheDocument()
      })

      it('open the search under More sources, and a pick selects like a tile', async () => {
        await openStep(SERVERS)
        fireEvent.click(screen.getByRole('button', { name: 'More sources' }))
        fireEvent.change(screen.getByLabelText('Search data sources'), { target: { value: 'opensearch' } })
        fireEvent.click(screen.getByRole('button', { name: /OpenSearch/ }))
        expect(await screen.findByText('Connect OpenSearch')).toBeInTheDocument()
        expect(screen.queryByLabelText('Search data sources')).not.toBeInTheDocument()
        // the pick takes the last source tile, so the selection stays visible
        expect(within(screen.getByRole('group', { name: 'Data source' })).getByRole('button', { name: /OpenSearch/ })).toHaveAttribute('aria-pressed', 'true')
      })

      it('say so when the search matches nothing', async () => {
        await openStep(SERVERS)
        fireEvent.click(screen.getByRole('button', { name: 'More sources' }))
        fireEvent.change(screen.getByLabelText('Search data sources'), { target: { value: 'zzz' } })
        expect(screen.getByText(/No data sources match/)).toBeInTheDocument()
      })

      it('show a retry when the server list cannot load', async () => {
        vi.mocked(mcpApi.listServers).mockRejectedValueOnce(new Error('down'))
        renderSetup()
        fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
        expect(await screen.findByText(/Couldn.t load available sources/)).toBeInTheDocument()
        expect(screen.getByRole('button', { name: /Upload a file/ })).toBeInTheDocument()
      })
    })

    describe('the connect card', () => {
      it('has no outer step card, and shows the sub-line, docs link and secrets note', async () => {
        await openStep()
        expect(screen.queryByText('Connect data')).not.toBeInTheDocument()
        expect(screen.getByText('Connect CrowdStrike Falcon')).toBeInTheDocument()
        expect(screen.getByText(/Vigil reads alerts only\. Tools that change something/)).toBeInTheDocument()
        expect(screen.getByText('Secrets are stored encrypted and never shown again.')).toBeInTheDocument()
        expect(screen.getByRole('link', { name: /Where do I find this/ })).toHaveAttribute(
          'href',
          expect.stringContaining('crowdstrike.com'),
        )
      })

      it('keeps sectioned fields under a collapsed More settings', async () => {
        await openStep(['splunk-selfhosted'])
        expect(screen.queryByLabelText(/^Proxy Host/)).not.toBeInTheDocument()
        fireEvent.click(screen.getByRole('button', { name: /More settings/ }))
        expect(screen.getByLabelText(/^Proxy Host/)).toBeInTheDocument()
      })

      it('blocks Test while a required field is empty', async () => {
        await openStep()
        fireEvent.change(screen.getByLabelText('Client ID'), { target: { value: 'id-1' } })
        testConnection()
        expect(await screen.findByRole('alert')).toHaveTextContent('Please fill in: Client Secret')
        expect(configApi.setIntegrations).not.toHaveBeenCalled()
        expect(screen.queryByRole('list', { name: 'Connection checks' })).not.toBeInTheDocument()
      })

      it('shows a failed connect as rows with no banner, then reads Test again', async () => {
        failWith({ error: 'Bad credentials' })
        await openStep()
        fillCrowdStrike()
        testConnection()
        const rows = await screen.findByRole('list', { name: 'Connection checks' })
        expect(within(rows).getByText('Bad credentials')).toBeInTheDocument()
        expect(within(rows).getByRole('img', { name: 'Needs you' })).toBeInTheDocument()
        expect(screen.queryByText(/^Connected\./)).not.toBeInTheDocument()
        expect(screen.queryByRole('switch', { name: 'Collect alerts' })).not.toBeInTheDocument()
        expect(screen.getByRole('button', { name: 'Test again' })).toBeEnabled()
        // the server is turned back off and the source does not count as enabled
        expect(mcpApi.setServerEnabled).toHaveBeenLastCalledWith('crowdstrike', false)
        expect(vi.mocked(configApi.setIntegrations).mock.lastCall?.[0]).toMatchObject({
          enabled_integrations: [],
        })
      })

      it('lists the missing credentials the server names', async () => {
        failWith({ missing_credentials: ['CLIENT_ID', 'CLIENT_SECRET'] })
        await openStep()
        fillCrowdStrike()
        testConnection()
        expect(await screen.findByText('Missing required credentials: CLIENT_ID, CLIENT_SECRET.')).toBeInTheDocument()
      })

      it('says the result could not be confirmed when the MCP service cannot say, with no banner', async () => {
        vi.mocked(mcpApi.setServerEnabled).mockResolvedValue({ data: { connected: null } } as never)
        await openStep()
        fillCrowdStrike()
        testConnection()
        expect(await screen.findByText(/could not be confirmed/)).toBeInTheDocument()
        expect(screen.queryByText(/^Connected\./)).not.toBeInTheDocument()
        expect(mcpApi.setServerEnabled).toHaveBeenCalledTimes(1)
      })

      it('shows a rejected save as a row too', async () => {
        vi.mocked(configApi.setIntegrations).mockRejectedValueOnce(new Error('Network Error'))
        await openStep()
        fillCrowdStrike()
        testConnection()
        expect(await screen.findByText('Network Error')).toBeInTheDocument()
        expect(screen.queryByText(/^Connected\./)).not.toBeInTheDocument()
      })

      it('shows the banner only after a pass, and Continue advances the step', async () => {
        await connect()
        expect(screen.getByText('Connected. Alerts from CrowdStrike Falcon flow into triage as soon as you finish setup.')).toBeInTheDocument()
        const banner = screen.getByText(/^Connected\. Alerts from/).closest('.su-banner') as HTMLElement
        fireEvent.click(within(banner).getByRole('button', { name: 'Continue' }))
        expect(await screen.findByText('Step 3 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
        expect(saved()).toEqual({ furthest: 3, passed: [1, 2] })
      })

      it('Connect another clears the card so another source can be picked', async () => {
        await connect()
        fireEvent.click(screen.getByRole('button', { name: 'Connect another' }))
        expect(screen.queryByText('Connect CrowdStrike Falcon')).not.toBeInTheDocument()
        fireEvent.click(screen.getByRole('button', { name: /CrowdStrike Falcon/ }))
        expect(screen.getByText('Connect CrowdStrike Falcon')).toBeInTheDocument()
      })

      it('Test again keeps what the first save recorded, so a later failure does not undo it', async () => {
        await connect()
        failWith({ error: 'Token expired' })
        testConnection()
        expect(await screen.findByText('Token expired')).toBeInTheDocument()
        // two saves; no third write reverting enabled_integrations, as it was already enabled
        expect(configApi.setIntegrations).toHaveBeenCalledTimes(2)
        expect(vi.mocked(configApi.setIntegrations).mock.calls[1][0]).toMatchObject({
          enabled_integrations: ['crowdstrike'],
        })
      })

      it('counts a secret it just saved when the field is cleared and tested again', async () => {
        await connect()
        fireEvent.change(screen.getByLabelText('Client Secret'), { target: { value: '' } })
        testConnection()
        await waitFor(() => expect(mcpApi.setServerEnabled).toHaveBeenCalledTimes(2))
        expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      })

      it('counts a stored secret as filled and never sends it back from memory', async () => {
        vi.mocked(configApi.getIntegrations).mockResolvedValue({
          data: {
            enabled_integrations: [],
            integrations: { crowdstrike: { client_id: 'id-1' } },
            secrets_set: { crowdstrike: { client_secret: true } },
          },
        } as never)
        await openStep()
        expect(screen.getByLabelText('Client Secret')).toHaveAttribute('placeholder', expect.stringContaining('saved'))
        testConnection()
        await screen.findByText('Connected to CrowdStrike Falcon')
      })
    })

    describe('collection settings after a pass', () => {
      it('show Collect alerts, the interval and Test under the rows', async () => {
        await connect()
        expect(await screen.findByRole('switch', { name: 'Collect alerts' })).toBeChecked()
        expect(screen.getByRole('button', { name: 'Test' })).toBeEnabled()
        expect(screen.getByLabelText('Interval (s)')).toBeInTheDocument()
      })

      it('stay while Test again runs', async () => {
        await connect()
        await screen.findByRole('switch', { name: 'Collect alerts' })
        let release: (v: unknown) => void = () => {}
        vi.mocked(mcpApi.setServerEnabled).mockReturnValue(new Promise((r) => (release = r)) as never)
        testConnection()
        expect(await screen.findByText('Connecting to CrowdStrike Falcon…')).toBeInTheDocument()
        expect(screen.getByRole('switch', { name: 'Collect alerts' })).toBeInTheDocument()
        release({ data: { connected: true } })
        await screen.findByText('Connected to CrowdStrike Falcon')
      })

      it('save the collection switch through updateSource', async () => {
        vi.mocked(federationApi.updateSource).mockResolvedValue({ data: source({ enabled: false }) } as never)
        await connect()
        fireEvent.click(await screen.findByRole('switch', { name: 'Collect alerts' }))
        expect(federationApi.updateSource).toHaveBeenCalledWith('crowdstrike', { enabled: false })
        expect(await screen.findByRole('button', { name: 'Test' })).toBeDisabled()
        expect(screen.getByText('Turn on Collect alerts to test.')).toBeInTheDocument()
      })

      it('save a typed interval on blur, and ignore a blank one', async () => {
        vi.mocked(federationApi.updateSource).mockResolvedValue({ data: source({ interval_seconds: 600 }) } as never)
        await connect()
        const input = await screen.findByLabelText('Interval (s)')
        fireEvent.change(input, { target: { value: '' } })
        fireEvent.blur(input)
        expect(federationApi.updateSource).not.toHaveBeenCalled()
        fireEvent.change(input, { target: { value: '600' } })
        fireEvent.blur(input)
        expect(federationApi.updateSource).toHaveBeenCalledWith('crowdstrike', { interval_seconds: 600 })
      })

      it('say collection is off globally and turn it on', async () => {
        vi.mocked(federationApi.listSources).mockResolvedValue(listing([source()], false))
        vi.mocked(federationApi.setSettings).mockResolvedValue({ data: { enabled: true } } as never)
        await connect()
        expect(await screen.findByText(/Alert collection is off/)).toBeInTheDocument()
        expect(screen.getByRole('button', { name: 'Test' })).toBeDisabled()
        fireEvent.click(screen.getByRole('switch', { name: 'Alert collection' }))
        expect(federationApi.setSettings).toHaveBeenCalledWith(true)
      })

      it('Test adds a Checking first-poll row, then Passed with last_success_at once the poll advanced', async () => {
        await connect()
        await screen.findByRole('switch', { name: 'Collect alerts' })
        vi.useFakeTimers()
        vi.mocked(federationApi.listSources)
          .mockResolvedValueOnce(listing([source()])) // baseline read before queueing
          .mockResolvedValueOnce(listing([source()])) // stale: poll has not run yet
          .mockResolvedValue(
            listing([source({ last_poll_at: '2026-10-06T10:05:00Z', last_success_at: '2026-10-06T10:05:00Z' })]),
          )
        await act(async () => {
          fireEvent.click(screen.getByRole('button', { name: 'Test' }))
        })
        expect(federationApi.pollNow).toHaveBeenCalledWith('crowdstrike')
        const poll = () => screen.getByText('First poll').closest('[role="listitem"]') as HTMLElement
        expect(within(poll()).getByRole('img', { name: 'Checking' })).toBeInTheDocument()
        await act(() => vi.advanceTimersByTimeAsync(TEST_POLL_MS))
        expect(screen.queryByText(/Last success/)).not.toBeInTheDocument()
        await act(() => vi.advanceTimersByTimeAsync(TEST_POLL_MS))
        expect(screen.getByText(/Last success/)).toBeInTheDocument()
        expect(within(poll()).getByRole('img', { name: 'Passed' })).toBeInTheDocument()
      })

      it('Test shows last_error as a Needs you row', async () => {
        await connect()
        await screen.findByRole('switch', { name: 'Collect alerts' })
        vi.useFakeTimers()
        vi.mocked(federationApi.listSources)
          .mockResolvedValueOnce(listing([source()])) // baseline read before queueing
          .mockResolvedValue(listing([source({ last_poll_at: '2026-10-06T10:05:00Z', last_error: '401 Unauthorized' })]))
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

      it('show the load error with a retry', async () => {
        vi.mocked(federationApi.listSources).mockRejectedValue(new Error('boom'))
        await connect()
        expect(await screen.findByText(/Couldn.t load collection settings: boom/)).toBeInTheDocument()
      })

      it('are left out for LogLM, which has no federation source', async () => {
        await openStep(['loglm'])
        fireEvent.change(screen.getByLabelText(/^Connector URL/), { target: { value: 'https://loglm.test' } })
        fireEvent.change(screen.getByLabelText(/^Session Mint Secret/), { target: { value: 's1' } })
        fireEvent.change(screen.getByLabelText(/^MCP Bearer Token/), { target: { value: 's2' } })
        vi.mocked(federationApi.listSources).mockClear()
        testConnection()
        expect(await screen.findByText('Connected to LogLM')).toBeInTheDocument()
        expect(screen.getByText(/^Connected\. Alerts from LogLM/)).toBeInTheDocument()
        expect(screen.queryByRole('switch')).not.toBeInTheDocument()
        expect(federationApi.listSources).not.toHaveBeenCalled()
      })
    })

    describe('Upload a file', () => {
      const job = (over: Record<string, unknown> = {}) => ({
        job_id: 'j1',
        filename: 'export.csv',
        format: 'csv',
        data_type: 'finding',
        status: 'running',
        determinate: false,
        processed: 0,
        total: 0,
        created_at: '2026-10-06T10:00:00Z',
        finished_at: null,
        message: '',
        error: null,
        stats: {},
        ...over,
      })
      const choose = () => {
        fireEvent.click(screen.getByRole('button', { name: /Upload a file/ }))
        return screen.getByTestId('setup-upload-input')
      }
      const file = () => new File(['a,b'], 'export.csv', { type: 'text/csv' })

      it('shows no row for a job that already existed', async () => {
        vi.mocked(ingestionApi.listJobs).mockResolvedValue({ data: [job({ status: 'failed', error: 'old' })] } as never)
        await openStep()
        choose()
        expect(screen.queryByRole('list', { name: 'Upload status' })).not.toBeInTheDocument()
      })

      it('shows the job this session started succeed', async () => {
        vi.mocked(ingestionApi.listJobs).mockResolvedValue({ data: [] } as never)
        vi.mocked(ingestionApi.uploadFile).mockResolvedValue({
          data: job({ status: 'succeeded', message: 'Imported 12 findings' }),
        } as never)
        await openStep()
        fireEvent.change(choose(), { target: { files: [file()] } })
        expect(await screen.findByText('Imported 12 findings')).toBeInTheDocument()
        expect(screen.getByRole('img', { name: 'Passed' })).toBeInTheDocument()
      })

      it('shows a failed job with its error', async () => {
        vi.mocked(ingestionApi.uploadFile).mockResolvedValue({
          data: job({ status: 'failed', error: 'Unsupported column types' }),
        } as never)
        await openStep()
        fireEvent.change(choose(), { target: { files: [file()] } })
        expect(await screen.findByText('Unsupported column types')).toBeInTheDocument()
        expect(screen.getByRole('img', { name: 'Needs you' })).toBeInTheDocument()
      })

      it('shows the error when the upload request is rejected', async () => {
        vi.mocked(ingestionApi.uploadFile).mockRejectedValue(new Error('File too large'))
        await openStep()
        fireEvent.change(choose(), { target: { files: [file()] } })
        expect(await screen.findByText('File too large')).toBeInTheDocument()
      })
    })

    describe('Try demo data', () => {
      const open = async () => {
        await openStep()
        fireEvent.click(screen.getByRole('button', { name: /Try demo data/ }))
      }

      it('turns demo mode on, shows Demo data on and stays in the wizard', async () => {
        await open()
        fireEvent.click(screen.getByRole('button', { name: 'Turn on demo data' }))
        expect(await screen.findByText('Demo data on', { selector: '.su-check-detail' })).toBeInTheDocument()
        expect(configApi.setDemoMode).toHaveBeenCalledWith(true)
        expect(screen.getByText('Step 2 of 5', { selector: '.su-eyebrow' })).toBeInTheDocument()
        expect(localStorage.getItem(SETUP_DISMISSED_KEY)).toBeNull()
      })

      it('does not override a server-set DEMO_MODE=false', async () => {
        vi.mocked(configApi.getDemoMode).mockResolvedValueOnce({
          data: { enabled: false, source: 'environment' },
        } as never)
        await open()
        fireEvent.click(screen.getByRole('button', { name: 'Turn on demo data' }))
        expect(await screen.findByText("Demo mode is set by the server's environment")).toBeInTheDocument()
        expect(screen.getByRole('img', { name: 'Needs you' })).toBeInTheDocument()
        expect(configApi.setDemoMode).not.toHaveBeenCalled()
      })

      it('says so when demo data cannot be turned on', async () => {
        vi.mocked(configApi.setDemoMode).mockRejectedValueOnce(new Error('500'))
        await open()
        fireEvent.click(screen.getByRole('button', { name: 'Turn on demo data' }))
        expect(await screen.findByText('Demo data could not be turned on')).toBeInTheDocument()
      })
    })
  })
})
