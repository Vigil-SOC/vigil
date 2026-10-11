import { beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import IntegrationsSection from './IntegrationsSection'
import { tabFromQuery } from './integrationsData'
import { IntegrationsStateProvider } from './IntegrationsState'
import { configApi, mcpApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  default: {},
  aiConfigApi: {},
  budgetsApi: {},
  configApi: {
    getIntegrations: vi.fn(() => Promise.resolve({ data: {} })),
    setIntegrations: vi.fn(),
  },
  detectionRulesApi: {
    listSources: vi.fn(() => Promise.resolve({ data: { sources: [] } })),
    getStats: vi.fn(() => Promise.resolve({ data: {} })),
  },
  federationApi: {},
  ingestionApi: {},
  kafkaApi: {},
  localServicesApi: {},
  mcpApi: {
    listServers: vi.fn(),
    getStatuses: vi.fn(),
    setServerEnabled: vi.fn(),
  },
  orchestratorApi: {},
  storageApi: {},
}))

function renderSection(path = '/settings?section=integrations') {
  render(
    <MemoryRouter initialEntries={[path]}>
      <IntegrationsStateProvider>
        <IntegrationsSection notify={vi.fn()} />
      </IntegrationsStateProvider>
    </MemoryRouter>,
  )
}

function row(name: string) {
  const cell = screen.getByText(name).closest('tr')
  if (!cell) throw new Error(`no row for ${name}`)
  return within(cell as HTMLElement)
}

const NOW = Date.now()
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString()

describe('Integrations: Connected table', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve({ json: async () => ({}) })),
    )
    vi.mocked(configApi.getIntegrations).mockResolvedValue({
      data: {
        enabled_integrations: ['github', 'virustotal'],
        integrations: {},
        secrets_set: {},
        last_test: {
          github: { at: iso(5 * 60_000), success: true, error: null },
          virustotal: { at: iso(3 * 3600_000), success: false, error: 'HTTP 403 from VirusTotal' },
        },
      },
    } as never)
    vi.mocked(mcpApi.listServers).mockResolvedValue({
      data: { servers: ['github', 'virustotal', 'security-detections', 'okta'] },
    } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({
      data: {
        statuses: [
          { name: 'github', status: 'running', enabled: true },
          {
            name: 'virustotal',
            status: 'disconnected',
            enabled: true,
            error: 'connection refused',
            missing_credentials: ['VT_API_KEY'],
          },
          { name: 'security-detections', status: 'disconnected', enabled: false },
          { name: 'okta', status: 'disconnected', enabled: false, missing_credentials: ['OKTA_API_TOKEN'] },
        ],
      },
    } as never)
  })

  it('reads health from the session and the last test, and counts what needs attention', async () => {
    renderSection()
    expect(await screen.findByRole('tab', { name: 'Connected 2' })).toHaveAttribute('aria-selected', 'true')

    const good = row('Github')
    expect(good.getByText('Good')).toBeInTheDocument()
    expect(good.getByText('5 min ago')).toHaveAttribute('title', expect.stringContaining('T'))

    const poor = row('Virustotal')
    expect(poor.getByText('Poor')).toBeInTheDocument()
    expect(poor.getByText('3 h ago')).toBeInTheDocument()
    expect(poor.getByText('Missing VT_API_KEY — connection refused — HTTP 403 from VirusTotal')).toBeInTheDocument()
    expect(poor.getByRole('button', { name: 'Fix' })).toBeInTheDocument()

    // tiles: Connected, Healthy, Need attention
    const tile = (k: string) => screen.getByText(k, { selector: '.int-tile-k' }).closest('.int-tile') as HTMLElement
    expect(tile('Connected')).toHaveTextContent('2')
    expect(tile('Healthy')).toHaveTextContent('1')
    expect(tile('Need attention')).toHaveTextContent('1')

    // the banner names the first failing integration
    expect(screen.getByRole('status')).toHaveTextContent('Virustotal needs a fix.')
    expect(screen.getByRole('status')).toHaveTextContent('HTTP 403 from VirusTotal')
  })

  it('keeps unconnected servers off the table and offers them under Add integration', async () => {
    renderSection()
    await screen.findByRole('tab', { name: 'Connected 2' })
    expect(screen.queryByText('Security Detections')).not.toBeInTheDocument()
    expect(screen.queryByText('Okta')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Add integration/ }))
    expect(screen.getByText('Security Detections')).toBeInTheDocument()
    // an unconnected server is not "Poor" for the credentials it was never given
    expect(screen.queryByText('Poor')).not.toBeInTheDocument()
    expect(screen.getByText('Okta')).toBeInTheDocument()
  })

  it('keeps a row on Connected, shown Off, when it is switched off, and switches back on', async () => {
    let on = true
    const statuses = () => ({
      data: {
        statuses: [
          { name: 'github', status: 'running', enabled: true },
          { name: 'security-detections', status: on ? 'running' : 'disconnected', enabled: on },
        ],
      },
    })
    vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: ['github', 'security-detections'] } } as never)
    vi.mocked(mcpApi.getStatuses).mockImplementation((() => Promise.resolve(statuses())) as never)
    vi.mocked(mcpApi.setServerEnabled).mockImplementation(((_n: string, want: boolean) => {
      on = want
      return Promise.resolve({ data: { connected: want } })
    }) as never)
    renderSection()
    await screen.findByRole('tab', { name: 'Connected 2' })

    fireEvent.click(row('Security Detections').getByRole('switch', { name: 'Toggle security-detections' }))
    await waitFor(() => expect(mcpApi.setServerEnabled).toHaveBeenCalledWith('security-detections', false))
    await waitFor(() => expect(row('Security Detections').getByText('Off')).toBeInTheDocument())
    expect(screen.getByRole('tab', { name: 'Connected 2' })).toBeInTheDocument()

    fireEvent.click(row('Security Detections').getByRole('switch', { name: 'Toggle security-detections' }))
    await waitFor(() => expect(mcpApi.setServerEnabled).toHaveBeenLastCalledWith('security-detections', true))
    await waitFor(() => expect(row('Security Detections').getByText('Good')).toBeInTheDocument())
  })

  it('shows a dash, not a level, where nothing was ever tested', async () => {
    vi.mocked(configApi.getIntegrations).mockResolvedValue({
      data: { enabled_integrations: ['github'], integrations: {}, secrets_set: {}, last_test: {} },
    } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({
      data: { statuses: [{ name: 'github', status: 'running', enabled: true }] },
    } as never)
    renderSection()
    await screen.findByRole('tab', { name: /Connected/ })
    expect(row('Github').getByText('—')).toHaveAttribute('title', 'Never tested')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('says so when nothing is connected', async () => {
    vi.mocked(configApi.getIntegrations).mockResolvedValue({ data: {} } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({ data: { statuses: [] } } as never)
    renderSection()
    expect(await screen.findByText('Nothing is connected yet')).toBeInTheDocument()
    // the head button of the same name sits above; the empty state's is the one inside the section body
    const empty = screen.getByText('Nothing is connected yet').closest('.empty-state') as HTMLElement
    fireEvent.click(within(empty).getByRole('button', { name: /Add integration/ }))
    expect(screen.getByRole('tab', { name: /Add integration/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('switches the tab strip from the page-head actions', async () => {
    renderSection()
    await screen.findByRole('tab', { name: 'Connected 2' })
    const head = within(screen.getByRole('banner'))
    fireEvent.click(head.getByRole('button', { name: 'Add integration' }))
    expect(screen.getByRole('tab', { name: /^Add integration/ })).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(head.getByRole('button', { name: 'Build custom' }))
    expect(screen.getByRole('tab', { name: /^Custom/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('shows loading, then an error with Retry', async () => {
    vi.mocked(mcpApi.listServers).mockRejectedValue(new Error('boom'))
    renderSection()
    expect(screen.getByText('Loading integrations…')).toBeInTheDocument()
    expect(await screen.findByText('Couldn’t load MCP servers')).toBeInTheDocument()
    vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: [] } } as never)
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
    await waitFor(() => expect(screen.queryByText('Couldn’t load MCP servers')).not.toBeInTheDocument())
  })
})

describe('Integrations: Custom tab', () => {
  const DRAFT = {
    success: true,
    integration_id: 'custom-acme',
    integration_name: 'Acme XDR',
    metadata: {
      category: 'EDR/XDR',
      description: 'Reads Acme findings.',
      fields: [
        { name: 'base_url', label: 'Base URL', type: 'url' },
        { name: 'api_key', label: 'API key', type: 'password' },
      ],
    },
    tools: [{ name: 'acme_list_findings', description: 'List findings since a time' }],
    server_code: 'print("v1")',
  }
  let listed: number
  let generateReply: () => unknown
  let calls: { url: string; body: Record<string, unknown> | null }[]

  const called = (suffix: string) => calls.filter((c) => c.url.endsWith(suffix))

  beforeEach(() => {
    listed = 1
    calls = []
    generateReply = () => DRAFT
    vi.mocked(configApi.getIntegrations).mockResolvedValue({ data: {} } as never)
    vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: [] } } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({ data: { statuses: [] } } as never)
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: { body?: string }) => {
        calls.push({ url, body: init?.body ? JSON.parse(init.body) : null })
        const ok = (body: unknown) => ({ ok: true, json: async () => body })
        if (url.endsWith('/list')) {
          if (listed < 0) return { ok: false, json: async () => ({}) }
          return ok({ success: true, integrations: Array.from({ length: listed }, (_, i) => ({ id: `c${i}` })) })
        }
        if (url.endsWith('/generate')) {
          const r = generateReply() as { fail?: string }
          return r.fail ? { ok: false, json: async () => ({ detail: r.fail }) } : ok(r)
        }
        if (url.endsWith('/save')) {
          listed += 1
          return ok({ success: true })
        }
        if (url.endsWith('/validate')) return ok({ valid: true, checks: { has_server: true, has_main: true } })
        return ok({})
      }),
    )
  })

  const open = async (n = 'Custom 1') => {
    renderSection('/settings?section=integrations&tab=custom')
    await screen.findByRole('tab', { name: n })
  }
  const generate = async () => {
    fireEvent.change(screen.getByLabelText(/^API documentation/), { target: { value: 'GET /v2/findings' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate draft' }))
    await screen.findByText('acme_list_findings')
  }

  it('takes the tab count from the saved list, and shows no count when the list is refused', async () => {
    listed = 3
    await open('Custom 3')
    cleanup()
    listed = -1
    const before = called('/list').length
    renderSection('/settings?section=integrations&tab=custom')
    await waitFor(() => expect(called('/list').length).toBeGreaterThan(before))
    expect(screen.getByRole('tab', { name: 'Custom' })).toBeInTheDocument()
  })

  it('starts empty: nothing drafted, Generate off until there is documentation', async () => {
    await open()
    expect(screen.getByText(/Nothing drafted yet/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Generate draft' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: /Save as draft|Test read-only/ })).not.toBeInTheDocument()
  })

  it('shows loading while Vigil drafts, then the draft: settings, tools and editable code', async () => {
    let finish: (v: unknown) => void = () => {}
    const pending = new Promise((r) => { finish = r })
    const base = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation(async (url, init) =>
      String(url).endsWith('/generate') ? ((await pending, base(url, init)) as never) : (base(url, init) as never),
    )
    await open()
    fireEvent.change(screen.getByLabelText(/^API documentation/), { target: { value: 'GET /v2/findings' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate draft' }))
    expect(await screen.findByText(/Vigil is reading the documentation/)).toBeInTheDocument()
    finish(null)

    expect(await screen.findByText('acme_list_findings')).toBeInTheDocument()
    expect(screen.getByText('List findings since a time')).toBeInTheDocument()
    expect(screen.getByText('Base URL')).toBeInTheDocument()
    expect(screen.getByText('API key (secret)')).toBeInTheDocument()
    expect(screen.getByLabelText('Server code')).toHaveValue('print("v1")')
    expect(screen.queryByText(/Claude/)).not.toBeInTheDocument()
  })

  it('says why when generating fails', async () => {
    generateReply = () => ({ fail: 'Claude API is not configured.' })
    await open()
    fireEvent.change(screen.getByLabelText(/^API documentation/), { target: { value: 'docs' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate draft' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('not configured')
    expect(screen.getByRole('button', { name: 'Generate draft' })).toBeEnabled()
  })

  it('asks the clarifying question and sends the answer back', async () => {
    generateReply = () => ({ success: true, needs_clarification: true, message: 'Which auth scheme?', conversation_history: [{ role: 'user', content: 'p' }] })
    await open()
    fireEvent.change(screen.getByLabelText(/^API documentation/), { target: { value: 'docs' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate draft' }))
    expect(await screen.findByText('Which auth scheme?')).toBeInTheDocument()
    generateReply = () => DRAFT
    fireEvent.change(screen.getByLabelText(/^Your answer/), { target: { value: 'Bearer token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    expect(await screen.findByText('acme_list_findings')).toBeInTheDocument()
    expect(called('/generate')[1].body).toMatchObject({ user_response: 'Bearer token' })
  })

  it('Validate saves then checks, and the count follows; Save does not write the same code twice', async () => {
    const notify = vi.fn()
    render(
      <MemoryRouter initialEntries={['/settings?section=integrations&tab=custom']}>
        <IntegrationsStateProvider>
          <IntegrationsSection notify={notify} />
        </IntegrationsStateProvider>
      </MemoryRouter>,
    )
    await screen.findByRole('tab', { name: 'Custom 1' })
    await generate()

    fireEvent.click(screen.getByRole('button', { name: 'Validate' }))
    expect(await screen.findByText('The code passes the static check.')).toBeInTheDocument()
    expect(called('/save')).toHaveLength(1)
    expect(called('/custom-acme/validate')).toHaveLength(1)
    expect(await screen.findByRole('tab', { name: 'Custom 2' })).toBeInTheDocument()
    expect(screen.getByText(/Saved, not enabled/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('ok', expect.stringContaining('saved but not enabled')))
    expect(called('/save')).toHaveLength(1)
    expect(screen.getByText(/Nothing drafted yet/)).toBeInTheDocument()
  })

  it('sends code edited after Validate when saving', async () => {
    await open()
    await generate()
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }))
    await screen.findByText('The code passes the static check.')

    fireEvent.change(screen.getByLabelText('Server code'), { target: { value: 'print("v2")' } })
    expect(screen.queryByText('The code passes the static check.')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(called('/save')).toHaveLength(2))
    expect(called('/save')[1].body).toMatchObject({ integration_id: 'custom-acme', server_code: 'print("v2")' })
  })
})

describe('Integrations: Add integration tab', () => {
  const open = async () => {
    renderSection('/settings?section=integrations&tab=add')
    await screen.findByRole('group', { name: 'Category' })
  }
  const card = (name: string) => screen.getByText(name, { selector: '.int-card-name' }).closest('.int-card') as HTMLElement

  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ json: async () => ({}) })))
    vi.mocked(configApi.getIntegrations).mockResolvedValue({
      data: { enabled_integrations: ['github'], integrations: {}, secrets_set: {}, last_test: {} },
    } as never)
    vi.mocked(mcpApi.listServers).mockResolvedValue({
      data: { servers: ['github', 'okta', 'virustotal', 'security-detections', 'slack'] },
    } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({
      data: {
        statuses: [
          { name: 'github', status: 'running', enabled: true },
          { name: 'okta', status: 'disconnected', enabled: false },
          { name: 'virustotal', status: 'disconnected', enabled: false },
          { name: 'security-detections', status: 'disconnected', enabled: false },
          { name: 'slack', status: 'disconnected', enabled: false },
        ],
      },
    } as never)
  })

  it('lists a chip per category without counts, marks connected cards and leaves WIP servers out', async () => {
    await open()
    const chips = within(screen.getByRole('group', { name: 'Category' }))
    // Slack is work in progress: no card
    expect(screen.queryByText('Slack', { selector: '.int-card-name' })).not.toBeInTheDocument()
    expect(chips.getByRole('button', { name: 'All' })).toHaveTextContent(/^All$/)
    expect(chips.getByRole('button', { name: 'Identity & Access' })).toHaveTextContent(/^Identity & Access$/)
    expect(chips.getByRole('button', { name: 'Incident Management' })).toBeInTheDocument()
    expect(within(card('GitHub')).getByText('Connected')).toBeInTheDocument()
    expect(within(card('GitHub')).queryByRole('button', { name: /Connect/ })).not.toBeInTheDocument()
    expect(within(card('Okta')).getByText('Available')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Add integration/ })).toHaveAccessibleName(/^Add integration \d+$/)
  })

  it('filters by chip and search', async () => {
    await open()
    fireEvent.click(within(screen.getByRole('group', { name: 'Category' })).getByRole('button', { name: /Threat Intelligence/ }))
    expect(screen.getByText('VirusTotal', { selector: '.int-card-name' })).toBeInTheDocument()
    expect(screen.queryByText('Okta', { selector: '.int-card-name' })).not.toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText('Search integrations…'), { target: { value: 'zzzz' } })
    expect(screen.getByText('No integrations match')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Clear filters/ }))
    expect(screen.getByText('Okta', { selector: '.int-card-name' })).toBeInTheDocument()
  })

  it('Connect opens the setup drawer; a server with no setup fields is turned on instead', async () => {
    vi.mocked(mcpApi.setServerEnabled).mockResolvedValue({ data: {} } as never)
    await open()
    fireEvent.click(within(card('Okta')).getByRole('button', { name: 'Connect Okta' }))
    expect(await screen.findByText('Add connection')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    fireEvent.click(within(card('Security Detections')).getByRole('button', { name: 'Turn on Security Detections' }))
    await waitFor(() => expect(mcpApi.setServerEnabled).toHaveBeenCalledWith('security-detections', true))
  })

  it('says so while loading and when it cannot load', async () => {
    vi.mocked(mcpApi.listServers).mockRejectedValue(new Error('boom'))
    renderSection('/settings?section=integrations&tab=add')
    expect(screen.getByText('Loading integrations…')).toBeInTheDocument()
    expect(await screen.findByText('Couldn’t load MCP servers')).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Category' })).not.toBeInTheDocument()
  })

  it('still offers the catalog when no server is running', async () => {
    vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: [] } } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({ data: { statuses: [] } } as never)
    await open()
    expect(within(card('Okta')).getByRole('button', { name: 'Connect Okta' })).toBeInTheDocument()
  })
})

describe('Integrations: tab key', () => {
  it('maps the old servers value to connected and keeps the rest', () => {
    expect(tabFromQuery('servers')).toBe('connected')
    expect(tabFromQuery('connected')).toBe('connected')
    expect(tabFromQuery('add')).toBe('add')
    expect(tabFromQuery('custom')).toBe('custom')
    expect(tabFromQuery('surface')).toBe('surface')
    expect(tabFromQuery('detection')).toBe('connected')
    expect(tabFromQuery(null)).toBe('connected')
  })

  it('opens the tab the query asks for, and Connected for a tab that lives elsewhere', () => {
    vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: [] } } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({ data: { statuses: [] } } as never)
    renderSection('/settings?section=integrations&tab=custom')
    expect(screen.getByRole('tab', { name: /Custom/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('stays on Connected for a tab that lives elsewhere', () => {
    vi.mocked(mcpApi.listServers).mockResolvedValue({ data: { servers: [] } } as never)
    vi.mocked(mcpApi.getStatuses).mockResolvedValue({ data: { statuses: [] } } as never)
    renderSection('/settings?section=integrations&tab=detection')
    expect(screen.getByRole('tab', { name: /Connected/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByRole('button', { name: 'Manual Upload' })).not.toBeInTheDocument()
  })
})
