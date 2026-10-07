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
    fireEvent.click(screen.getByRole('button', { name: /Add integration/ }))
    expect(screen.getByRole('tab', { name: /Add integration/ })).toHaveAttribute('aria-selected', 'true')
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
