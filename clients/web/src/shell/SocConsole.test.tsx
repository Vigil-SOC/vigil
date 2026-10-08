import { afterEach, beforeEach, describe, it, expect, beforeAll, vi } from 'vitest'
import { act, cleanup, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { ColorSchemeProvider } from '../contexts/ColorSchemeContext'
import SocConsole from './SocConsole'
import LandingRedirect from '../routing/LandingRedirect'
import { CONSOLE_TOUR_SEEN_KEY } from './consoleTourSeen'
// these resolve to the mocked implementations (vi.mock below is hoisted)
import api, { streamFetch, aiDecisionsApi, approvalsApi, workflowApi, configApi, consoleApi, timelineApi } from '../services/api'

const authState = vi.hoisted(() => ({
  allow: (_permission: string): boolean => true,
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { full_name: 'Test User', email: 'test@vigil.local', role_id: 'role-admin', mfa_enabled: false },
    logout: vi.fn(),
    hasPermission: (permission: string) => authState.allow(permission),
  }),
}))

// The console reads the scheme from ColorSchemeContext, so it needs a real one above it.
function ConsoleAt() {
  const location = useLocation()
  return (
    <>
      <div data-testid="console-location" data-path={location.pathname} data-search={location.search} />
      <SocConsole />
    </>
  )
}

function renderConsole(path = '/dashboard') {
  return render(
    <ColorSchemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/">
            <Route index element={<LandingRedirect />} />
            <Route path=":screen" element={<ConsoleAt />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </ColorSchemeProvider>,
  )
}

// Literals are inlined below because vi.mock is hoisted.
vi.mock('../services/api', () => ({
  casesApi: {
    getAll: () =>
      Promise.resolve({
        data: {
          cases: [
            { case_id: 'case-2026-0142', title: 'Defense Evasion: Obfuscated Loader', status: 'open', priority: 'high', assignee: 'j.reyes', finding_ids: ['f-1'], created_at: '2026-06-15T09:14:00Z' },
            { case_id: 'case-2026-0140', title: 'Ransomware Campaign — DataLock', status: 'investigating', priority: 'critical', assignee: 'soc-lead', finding_ids: ['f-1', 'f-2'], created_at: '2026-06-15T04:00:00Z' },
          ],
        },
      }),
    getById: (id: string) =>
      Promise.resolve({
        data: { case_id: id, title: 'Defense Evasion: Obfuscated Loader', status: 'open', priority: 'high', assignee: 'j.reyes', finding_ids: ['f-1'], created_at: '2026-06-15T09:14:00Z' },
      }),
    getSummary: () => Promise.resolve({ data: { total: 7, by_status: { open: 5, investigating: 1, closed: 1 } } }),
    getSLA: () => Promise.resolve({ data: {} }),
    getRecord: () => Promise.resolve({ data: { rows: [], run_id: null, investigation_id: null } }),
    getComments: () => Promise.resolve({ data: { comments: [] } }),
    getTasks: () => Promise.resolve({ data: { tasks: [] } }),
    getEvidence: () => Promise.resolve({ data: { evidence: [] } }),
    getIOCs: () => Promise.resolve({ data: { iocs: [] } }),
    getEscalations: () => Promise.resolve({ data: { escalations: [] } }),
  },
  exclusionsApi: {
    list: () => Promise.resolve({ data: { exclusions: [], total: 0 } }),
    create: vi.fn(),
    remove: vi.fn(),
  },
  findingsApi: {
    getAll: () =>
      Promise.resolve({
        data: {
          findings: [
            { finding_id: 'f-20260614-3b5c585e', severity: 'critical', data_source: 'firewall', timestamp: '2026-06-14T17:30:00Z', anomaly_score: 0.93, status: 'new', mitre_predictions: { 'T1567.002': 0.98 } },
          ],
        },
      }),
    getById: (id: string) =>
      Promise.resolve({
        data: { finding_id: id, severity: 'critical', data_source: 'edr', timestamp: '2026-06-14T17:30:00Z', mitre_predictions: { 'T1567.002': 0.98 } },
      }),
    getSummary: () => Promise.resolve({ data: { total: 40, by_severity: { critical: 7, high: 8, medium: 18, low: 7 } } }),
  },
  agentsApi: {
    listAgents: () =>
      Promise.resolve({
        data: {
          agents: [
            { id: 'triage', name: 'Triage Agent', specialization: 'Alert Triage', color: 'var(--high)' },
          ],
        },
      }),
  },
  claudeApi: {
    getModels: () => Promise.resolve({ data: { models: [{ id: 'claude-sonnet-4-6', name: 'Claude Sonnet 4.6' }] } }),
  },
  mcpApi: {
    getStatuses: () => Promise.resolve({
      data: {
        statuses: [
          { name: 'a', status: 'running', enabled: true },
          { name: 'b', status: 'running', enabled: true },
        ],
      },
    }),
  },
  federationApi: {
    getHealth: () => Promise.resolve({ data: { sources: [] } }),
  },
  consoleApi: {
    getHealth: vi.fn(() => Promise.resolve({
      data: { status: 'healthy', version: '1.2.3', storage: { database_available: true }, schema: { state: 'current' } },
    })),
    getRoutability: vi.fn(() => Promise.resolve({ data: { providers: { gemini: true } } })),
  },
  aiConfigApi: {
    getConfig: () => Promise.resolve({ data: { components: [], assignments: {} } }),
  },
  // a vi.fn, so the SSE test can supply a streaming body
  streamFetch: vi.fn(() => Promise.resolve({ ok: true, status: 200, body: null })),
  workflowApi: {
    listAll: () =>
      Promise.resolve({
        data: {
          workflows: [
            { id: 'incident-response', name: 'Incident Response', description: 'Respond to active incidents.', agents: ['triage', 'responder'], trigger_examples: ['"Run incident response"'], run_kind: 'compose' },
            { id: 'threat-hunt', name: 'Threat Hunt', description: 'Hunt.', agents: ['hunter'], run_kind: 'hunt' },
          ],
        },
      }),
    listRuns: vi.fn((id: string) =>
      Promise.resolve({
        data: {
          runs: id === 'incident-response'
            ? [{ run_id: 'r1', workflow_id: id, status: 'completed' }, { run_id: 'r2', workflow_id: id, status: 'failed' }]
            : [],
        },
      })),
    getRun: vi.fn(() => Promise.reject(new Error('no run'))),
  },
  // the bare client, for hooks that call routes without a named wrapper
  default: {
    get: vi.fn((path: string) =>
      path === '/analytics/cost'
        ? Promise.resolve({
            data: {
              totals: { calls: 12, input_tokens: 12000, output_tokens: 3400, cache_read_tokens: 0, cache_creation_tokens: 0, cost_usd: 1.25, cache_hit_rate: 0.1 },
              by_model: [{ model: 'gemini-2.5-flash', provider_type: 'vertex', pricing_source: 'exact', calls: 12, input_tokens: 12000, output_tokens: 3400, cost_usd: 1.25, cache_hit_rate: 0.1 }],
            },
          })
        : Promise.reject(new Error(`unmocked GET ${path}`))),
  },
  attackApi: {
    getTechniqueRollup: () =>
      Promise.resolve({
        data: { techniques: [{ technique_id: 'T1567.002', count: 10, severities: { critical: 2, high: 3, medium: 4, low: 1 } }] },
      }),
    getFindingsByTechnique: () => Promise.resolve({ data: { findings: [] } }),
  },
  timelineApi: {
    getTimelineRange: vi.fn((params: { start?: string; end?: string } = {}) => {
      const events = [
        { id: 'finding-f-1', start: '2026-06-12T11:36:33Z', type: 'finding', severity: 'medium', metadata: { finding_id: 'f-1' } },
        { id: 'finding-f-2', start: '2026-06-13T11:36:33Z', type: 'finding', severity: 'high', metadata: { finding_id: 'f-2' } },
      ]
      const start = params.start ? Date.parse(params.start) : Number.NEGATIVE_INFINITY
      const end = params.end ? Date.parse(params.end) : Number.POSITIVE_INFINITY
      return Promise.resolve({
        data: { events: events.filter((event) => Date.parse(event.start) >= start && Date.parse(event.start) <= end) },
      })
    }),
  },
  aiDecisionsApi: {
    getPendingFeedback: () =>
      Promise.resolve({
        data: [
          { decision_id: 'd-4471', agent_id: 'correlation', decision_type: 'Cluster merge', confidence_score: 0.96, reasoning: 'Shared host ws-eng-44 and a common C2 beacon interval.', recommended_action: 'Merge into case-2026-0140', workflow_id: 'f-20260614-3b5c585e', timestamp: '2026-06-14T17:42:00Z' },
        ],
      }),
    list: () => Promise.resolve({ data: [] }),
    getStats: () =>
      Promise.resolve({
        data: { total_decisions: 128, feedback_rate: 0.74, total_with_feedback: 95, agreement_rate: 0.91, avg_accuracy_grade: 0.8, total_time_saved_hours: 42, total_time_saved_minutes: 2520, period_days: 30, outcomes: { true_positive: 15, false_positive: 3 } },
      }),
    submitFeedback: vi.fn(() => Promise.resolve({})),
  },
  approvalsApi: {
    listPending: vi.fn(() => Promise.resolve({ data: { actions: [] } })),
    needsYou: vi.fn(() => Promise.resolve({ data: { count: 0, items: [] } })),
    approve: vi.fn(() => Promise.resolve({})),
    reject: vi.fn(() => Promise.resolve({})),
  },
  configApi: {
    getTheme: () => Promise.resolve({ data: { theme: 'dark' } }),
    setTheme: () => Promise.resolve({ data: {} }),
    getIntegrations: () => Promise.resolve({ data: { enabled_integrations: [] } }),
    getGeneral: () => Promise.resolve({ data: { show_notifications: false } }),
    getAutonomy: vi.fn(() => Promise.resolve({
      data: { auto_response_enabled: true, force_manual_approval: false },
    })),
    getDemoMode: vi.fn(() => Promise.resolve({ data: { enabled: false } })),
    getSetupSteps: vi.fn(() => Promise.resolve({
      data: { steps: [], alerts_exist: 1, demo_enabled: false },
    })),
  },
  orchestratorApi: {
    getStatus: () => Promise.resolve({ data: { enabled: false } }),
  },
  analyticsApi: {
    estimateCost: () =>
      Promise.resolve({
        data: {
          provider_type: 'anthropic',
          model_id: 'claude-sonnet-4-6',
          input_tokens: 0,
          output_tokens_max: 4096,
          low_usd: 0,
          high_usd: 0,
          pricing_source: 'exact',
          token_count_method: 'anthropic_count_tokens',
        },
      }),
  },
  reasoningApi: {
    getSessionSummary: () => Promise.resolve(null),
    listInteractions: () => Promise.resolve({ interactions: [] }),
    getInteraction: () => Promise.resolve({}),
  },
  triageApi: {
    get: () => Promise.resolve({
      data: {
        rows: [],
        strip: {
          picked_up: { launched_or_merged: 0, created_today: 0, share: null },
          waiting: 0,
          cases_created_today: 0,
          trust_floor: 'Not measured yet',
        },
        sources: [],
        arrival_info: 'Arrivals count every finding stored today. The list is the intake rows.',
        strip_info: {
          picked_up: { source: 'Picked source', calculation: 'Picked calc' },
          waiting: { source: 'Waiting source', calculation: 'Waiting calc' },
          cases_created_today: { source: 'Cases source', calculation: 'Cases calc' },
          trust_floor: { source: 'Floor source', calculation: 'Floor calc', limit: 'Floor limit' },
        },
        breakdown_info: { trust: 'Trust tip', weight: 'Weight tip', score: 'Score tip' },
        unmeasured_text: 'Not measured yet',
      },
    }),
  },
  overviewApi: {
    get: () => Promise.resolve({
      data: {
        day: '2026-10-01',
        empty: true,
        arrivals: [],
        engine: { source_text: '' },
        outcomes: [],
        running_source: '',
        step_source: '',
        rate_info: '',
        good_at: 0.95,
        fair_at: 0.85,
        agents: [],
        feed: [],
      },
    }),
  },
  conversationsApi: {
    list: () => Promise.resolve({ data: { conversations: [] } }),
    get: () => Promise.resolve({ data: { messages: [] } }),
    update: () => Promise.resolve({ data: {} }),
    delete: () => Promise.resolve({ data: {} }),
    importHistory: () => Promise.resolve({ data: { imported: 0, skipped: 0 } }),
  },
}))

vi.mock('../services/skillsApi', () => ({
  skillsApi: {
    list: () =>
      Promise.resolve([
        { skill_id: 's-1', name: 'UI Demo Skill', description: 'Demo skill.', category: 'custom', version: 1, is_active: true },
      ]),
  },
}))

// jsdom lacks ResizeObserver, which the interactive Timeline uses
beforeAll(() => {
  const g = globalThis as unknown as { ResizeObserver?: unknown }
  if (!g.ResizeObserver) {
    g.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  }
})

const defaultViewportWidth = window.innerWidth

afterEach(() => {
  authState.allow = () => true
  localStorage.clear()
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: defaultViewportWidth })
  vi.mocked(approvalsApi.listPending).mockResolvedValue({ data: { actions: [] } } as never)
  vi.mocked(consoleApi.getHealth).mockResolvedValue({
    data: { status: 'healthy', version: '1.2.3', storage: { database_available: true }, schema: { state: 'current' } },
  } as never)
  vi.mocked(consoleApi.getRoutability).mockResolvedValue({ data: { providers: { gemini: true } } } as never)
  vi.mocked(configApi.getAutonomy).mockResolvedValue({
    data: { auto_response_enabled: true, force_manual_approval: false },
  } as never)
  vi.mocked(configApi.getDemoMode).mockResolvedValue({ data: { enabled: false } } as never)
  vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 0, items: [] } } as never)
})

const title = () => screen.getByRole('heading', { level: 1 }).textContent
// Home draws its own headline, so the nav shows where the console is
const here = () => screen.getByRole('navigation', { name: 'Primary' }).querySelector('[aria-current="page"]')?.textContent

const MORE_LABELS = ['Dashboard', 'Case Metrics', 'Analytics', 'AI Decisions', 'Auto Ops', 'Health']

function clickScreen(name: string) {
  const inMore = MORE_LABELS.some((label) => name === label || name.startsWith(`${label} (`))
  if (inMore) {
    const more = screen.getByRole('button', { name: 'More' })
    if (more.getAttribute('aria-expanded') !== 'true') fireEvent.click(more)
  }
  fireEvent.click(screen.getByRole('button', { name }))
}

function domRect(top: number, left: number, width: number, height: number): DOMRect {
  return {
    x: left,
    y: top,
    top,
    left,
    width,
    height,
    bottom: top + height,
    right: left + width,
    toJSON() { return {} },
  } as DOMRect
}

describe('SocConsole', () => {
  beforeEach(() => {
    localStorage.setItem(CONSOLE_TOUR_SEEN_KEY, '1')
  })

  it('mounts on the Dashboard', () => {
    renderConsole()
    expect(title()).toBe('Dashboard')
    expect(screen.getByText('Security operations overview')).toBeInTheDocument()
  })

  it('puts Home first on the primary nav and opens it at /', async () => {
    renderConsole('/')
    await screen.findByRole('button', { name: 'Home' })
    expect(here()).toBe('Home')
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    const nav = screen.getByRole('navigation', { name: 'Primary' })
    const names = within(nav).getAllByRole('button').map((button) => button.getAttribute('aria-label'))
    expect(names.slice(0, 6)).toEqual(['Home', 'Overview', 'Triage queue', 'Cases', 'Agents & workflows', 'Settings'])
    expect(screen.getByRole('button', { name: 'Home' }).querySelector('.vg-nav-count')).toBeNull()
    expect(screen.getByRole('button', { name: 'Cases' }).querySelector('.vg-nav-count')).toBeNull()
  })

  it('paints the needs-you count on Home and Cases without opening the approvals tab', async () => {
    vi.mocked(approvalsApi.needsYou).mockResolvedValue({ data: { count: 120, items: [] } } as never)
    vi.mocked(approvalsApi.listPending).mockResolvedValue({
      data: {
        actions: [
          { action_id: 'a', action_type: 'isolate_host', title: 'isolate_host: host1', target: 'host1' },
        ],
      },
    } as never)

    renderConsole()

    const cases = await screen.findByRole('button', { name: 'Cases (120 waiting)' })
    expect(cases.querySelector('.vg-nav-count')).toHaveTextContent('99+')
    expect(screen.getByRole('button', { name: 'Home (120 waiting)' }).querySelector('.vg-nav-count')).toHaveTextContent('99+')
    fireEvent.click(cases)
    expect(title()).toBe('Cases')
    expect(screen.getByTestId('console-location')).toHaveAttribute('data-search', '')

    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    fireEvent.click(await screen.findByRole('button', { name: 'AI Decisions (1 waiting)' }))
    expect(await screen.findByRole('tab', { name: 'Pending Approvals (1)' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('console-location')).toHaveAttribute('data-search', '?tab=approvals')
    vi.mocked(approvalsApi.listPending).mockResolvedValue({ data: { actions: [] } } as never)
  })

  it('shows one demo banner only while demo mode is on', async () => {
    renderConsole()
    await screen.findByText('Security operations overview')
    expect(screen.queryByText('The data on screen is demo data.')).not.toBeInTheDocument()

    vi.mocked(configApi.getDemoMode).mockResolvedValue({ data: { enabled: true } } as never)
    renderConsole()
    expect(await screen.findByText('The data on screen is demo data.')).toBeInTheDocument()
  })

  it('renders the 404 screen for an unknown path and routes home', async () => {
    renderConsole('/does-not-exist')
    // an unknown path is probed as a page extension first, so the 404 body only
    // lands once that resolution settles
    expect(await screen.findByText('404')).toBeInTheDocument()
    // the screen's own h2 is the only heading
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('Page not found')
    fireEvent.click(screen.getByRole('button', { name: /Back to Home/ }))
    expect(here()).toBe('Home')
  })

  it('sends the 404 button to Overview for an operator without the Home permission', async () => {
    authState.allow = (permission: string) => permission !== 'ai_decisions.approve'
    renderConsole('/does-not-exist')
    fireEvent.click(await screen.findByRole('button', { name: /Back to Overview/ }))
    expect(title()).toBe('Overview')
  })

  it('offers Back to Home on Access denied', () => {
    authState.allow = (permission: string) => permission !== 'cases.read'
    renderConsole('/cases')
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('Access denied')
    fireEvent.click(screen.getByRole('button', { name: 'Back to Home' }))
    expect(here()).toBe('Home')
  })

  it('lands / on Home, or on Overview without the Home permission', () => {
    renderConsole('/')
    expect(here()).toBe('Home')
    cleanup()
    authState.allow = (permission: string) => permission !== 'ai_decisions.approve'
    renderConsole('/')
    expect(title()).toBe('Overview')
  })

  it('navigates primary screens and the More menu', () => {
    renderConsole()
    const screens: [string, string][] = [
      ['Cases', 'Cases'],
      ['Agents & workflows', 'Agents & workflows'],
      ['Settings', 'Settings'],
      ['Overview', 'Overview'],
      ['Triage queue', 'Triage queue'],
      ['Dashboard', 'Dashboard'],
      ['Case Metrics', 'Case Metrics'],
      ['Analytics', 'Analytics Dashboard'],
      ['AI Decisions', 'AI Decisions'],
      ['Auto Ops', 'Auto Ops'],
      ['Health', 'Health'],
    ]
    for (const [navLabel, pageTitle] of screens) {
      clickScreen(navLabel)
      expect(title()).toBe(pageTitle)
    }
    clickScreen('Home')
    expect(here()).toBe('Home')
  })

  it('marks the active tab, and More for a screen inside it', () => {
    renderConsole('/cases')
    const cases = screen.getByRole('button', { name: 'Cases' })
    expect(cases).toHaveAttribute('aria-current', 'page')
    expect(cases).toHaveClass('active')
    expect(screen.getByRole('button', { name: 'Overview' })).not.toHaveAttribute('aria-current')
    const more = screen.getByRole('button', { name: 'More' })
    expect(more).not.toHaveClass('active')
    clickScreen('Health')
    expect(screen.getByRole('button', { name: 'More' })).toHaveClass('active')
  })

  it('draws a rule between nav groups, never two together when Cases is hidden', () => {
    const rules = () =>
      Array.from(screen.getByRole('navigation', { name: 'Primary' }).children).map((el) =>
        el.classList.contains('vg-nav-sep') ? '|' : 'x',
      ).join('')
    const { unmount } = renderConsole('/overview')
    // Home | Overview | Triage queue Cases | Agents & workflows Settings | More | chips
    expect(rules()).toBe('x|x|xx|xx|xx')
    expect(screen.getAllByRole('navigation', { name: 'Primary' })[0].querySelectorAll('.vg-nav-sep')).toHaveLength(4)
    unmount()

    authState.allow = (permission: string) => permission !== 'cases.read'
    renderConsole('/overview')
    expect(rules()).toBe('x|x|x|xx|xx')
  })

  it('draws the board page heading in the screen, with the board copy', async () => {
    renderConsole('/overview')
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent('Overview')
    expect(screen.getByText('Where your data comes from, what Vigil does with it, and what came out.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 }).closest('.vg-page-head')?.parentElement).toHaveClass('screen')
    expect(document.querySelector('.topbar')).toBeNull()
    clickScreen('Cases')
    // the Cases list draws its own page head, with the same copy
    expect(title()).toBe('Cases')
    expect(screen.getByText(/^Every open case, who owns it and what it is waiting on\./)).toBeInTheDocument()
    clickScreen('Triage queue')
    expect(screen.getByText('Every incoming alert and what triage did with it.')).toBeInTheDocument()
  })

  it('shows no shell heading where the screen owns it', async () => {
    renderConsole('/')
    await screen.findByRole('button', { name: 'Home' })
    for (const label of ['Home', 'Agents & workflows', 'Settings']) {
      clickScreen(label)
      expect(screen.queryAllByRole('heading', { level: 1 }).length).toBeLessThanOrEqual(1)
      expect(document.querySelector('.vg-page-head')).toBeNull()
    }
  })

  it('shows Finish setup with the open step count and links to /setup', async () => {
    const step = (done: boolean) => ({ id: 'x', title: 'x', state_line: '', done, href: '/x' })
    const read = (...steps: boolean[]) =>
      vi.mocked(configApi.getSetupSteps).mockResolvedValue({
        data: { steps: steps.map(step), alerts_exist: 0, demo_enabled: false },
      } as never)

    read(true, false, false)
    const first = renderConsole()
    const chip = await screen.findByRole('link', { name: 'Finish setting up Vigil: 2 steps are left. Opens the setup guide.' })
    expect(chip).toHaveTextContent('Finish setup')
    expect(chip).toHaveTextContent('2 left')
    expect(chip).toHaveAttribute('href', '/setup')
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    expect(screen.getByRole('menuitem', { name: 'Setup guide 2 left' })).toBeInTheDocument()
    first.unmount()

    read(true, false)
    const second = renderConsole()
    expect(await screen.findByRole('link', { name: 'Finish setting up Vigil: 1 step is left. Opens the setup guide.' })).toHaveTextContent('1 left')
    // a step finished elsewhere shows on the next screen change
    read(true, true)
    clickScreen('Cases')
    await waitFor(() => expect(screen.queryByRole('link', { name: /Finish setting up Vigil/ })).not.toBeInTheDocument())
    second.unmount()

    vi.mocked(configApi.getSetupSteps).mockRejectedValue(new Error('boom'))
    renderConsole()
    await waitFor(() => expect(configApi.getSetupSteps).toHaveBeenCalled())
    expect(screen.queryByRole('link', { name: /Finish setting up Vigil/ })).not.toBeInTheDocument()
    vi.mocked(configApi.getSetupSteps).mockResolvedValue({
      data: { steps: [], alerts_exist: 1, demo_enabled: false },
    } as never)
  })

  it('puts the status chip in the nav row, with no separate strip', async () => {
    renderConsole()
    const chip = await screen.findByRole('status', { name: 'System status' })
    expect(chip.closest('nav')).toBe(screen.getByRole('navigation', { name: 'Primary' }))
    expect(chip).toHaveAttribute('title', 'No problems reported.')
    expect(document.querySelector('.vg-status')).toBeNull()
  })

  it('hides the nav row and the top bar while Overview is on the wall', async () => {
    renderConsole('/overview')
    expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Overview')
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument()
    expect(document.querySelector('[data-command-slot]')).toBeNull()
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Exit full screen' }))
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Overview')
  })

  it('shows spend, approval depth and recent run outcomes on the Health screen', async () => {
    renderConsole('/health')
    // once in the total KPI, once in the by-model row
    expect(await screen.findAllByText('$1.25')).toHaveLength(2)
    expect(screen.getByText('12.0k / 3.4k')).toBeInTheDocument()
    expect(screen.getByText('gemini-2.5-flash')).toBeInTheDocument()
    expect(await screen.findByText('Nothing waiting')).toBeInTheDocument()
    // runs are joined onto the workflow's declared kind; the hunt has none and is left out
    expect(await screen.findByText('compose · 1 workflow')).toBeInTheDocument()
    expect(screen.getByText('1 ok · 1 failed · 2 total')).toBeInTheDocument()
    expect(screen.queryByText(/^hunt ·/)).not.toBeInTheDocument()
  })

  it('renders the Health screen as clean empties when nothing has run or spent', async () => {
    const empty = { calls: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0, cost_usd: 0, cache_hit_rate: 0 }
    vi.mocked(api.get).mockResolvedValueOnce({ data: { totals: empty, by_model: [] } } as never)
    vi.mocked(workflowApi.listRuns).mockResolvedValue({ data: { runs: [] } } as never)
    try {
      renderConsole('/health')
      expect(await screen.findByText('No LLM traffic in this window')).toBeInTheDocument()
      expect(await screen.findByText('No runs yet')).toBeInTheDocument()
      expect(await screen.findByText('Nothing waiting')).toBeInTheDocument()
      // an empty deploy shows copy, never a zero dressed up as a metric
      expect(screen.queryByText('$0.00')).not.toBeInTheDocument()
    } finally {
      vi.mocked(workflowApi.listRuns).mockReset()
    }
  })

  it('counts approvals on the Health screen and sends the button to the approvals tab', async () => {
    vi.mocked(approvalsApi.listPending).mockResolvedValue({
      data: { actions: [{ action_id: 'a-1', title: 'Approve containment?' }, { action_id: 'a-2', title: 'Approve isolation?' }] },
    } as never)
    try {
      renderConsole('/health')
      // the count sits beside "pending"; the nav badge shows the same number
      expect((await screen.findByText('pending')).previousElementSibling?.textContent).toBe('2')
      fireEvent.click(screen.getByRole('button', { name: /Open approvals/ }))
      expect(title()).toBe('AI Decisions')
      expect(await screen.findByRole('tab', { name: /Pending Approvals/, selected: true })).toBeInTheDocument()
    } finally {
      vi.mocked(approvalsApi.listPending).mockResolvedValue({ data: { actions: [] } } as never)
    }
  })

  it('keeps the other kinds when one workflow’s runs cannot be read', async () => {
    vi.mocked(workflowApi.listRuns).mockImplementation(((id: string) =>
      id === 'threat-hunt'
        ? Promise.reject(new Error('boom'))
        : Promise.resolve({ data: { runs: [{ run_id: 'r1', workflow_id: id, status: 'completed' }, { run_id: 'r9', workflow_id: id, status: 'weird' }] } })) as never)
    try {
      renderConsole('/health')
      expect(await screen.findByText('compose · 1 workflow')).toBeInTheDocument()
      // an unknown status still counts toward the total rather than vanishing
      expect(screen.getByText('1 ok · 0 failed · 2 total')).toBeInTheDocument()
      expect(screen.getByText(/Runs for Threat Hunt couldn’t be read/)).toBeInTheDocument()
    } finally {
      vi.mocked(workflowApi.listRuns).mockReset()
    }
  })

  it('switches every Dashboard tab including the interactive Timeline', async () => {
    renderConsole()
    fireEvent.click(screen.getByRole('tab', { name: 'ATT&CK' }))
    expect(screen.getByText(/Techniques by occurrence/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Timeline' }))
    expect(await screen.findByText(/events$/)).toBeInTheDocument()
  })

  it('restores and clears versioned findings preferences', async () => {
    localStorage.setItem('soc.findings.filters.v1', JSON.stringify({
      severity: 'critical',
      source: 'any',
      hiddenColumns: null,
    }))
    renderConsole()
    await screen.findByText('f-20260614-3b5c585e')

    const filters = screen.getByRole('button', { name: /Filters/ })
    expect(filters).toHaveClass('has-filters')
    fireEvent.click(filters)
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }))

    await waitFor(() => expect(JSON.parse(localStorage.getItem('soc.findings.filters.v1') || '{}')).toEqual({
      severity: 'any',
      source: 'any',
      hiddenColumns: null,
    }))
  })

  it('opens the Cases master-detail and returns to the table', async () => {
    renderConsole()
    clickScreen('Cases')
    fireEvent.click(await screen.findByText('Defense Evasion: Obfuscated Loader'))
    // a row opens the drawer, which holds the case page alone
    const drawer = await screen.findByRole('dialog', { name: 'Case' })
    expect(within(drawer).getByRole('tab', { name: /Summary/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New case' })).toBeInTheDocument()
    fireEvent.click(within(drawer).getByRole('button', { name: 'Expand' }))
    expect(screen.queryByRole('dialog', { name: 'Case' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New case' })).not.toBeInTheDocument()
    expect(await screen.findByRole('tab', { name: /Summary/ })).toBeInTheDocument()
    fireEvent.click(screen.getByText('Cases', { selector: '.dh-crumb .back' }))
    expect(screen.getByRole('button', { name: 'New case' })).toBeInTheDocument()
  })

  describe('⌘K Tab', () => {
    const ask = (text: string) => {
      const input = screen.getByRole('combobox', { name: /Find a case/ })
      fireEvent.change(input, { target: { value: text } })
      fireEvent.keyDown(input, { key: 'Tab' })
    }
    const sentOnCase = async () => {
      await waitFor(() => expect(vi.mocked(streamFetch)).toHaveBeenCalled())
      const body = JSON.parse((vi.mocked(streamFetch).mock.calls[0][1] as { body: string }).body)
      expect(body.case_id).toBe('case-2026-0142')
      expect(body.messages.at(-1)).toEqual({ role: 'user', content: 'why this loader?' })
      expect(document.querySelector('.soc-console')).not.toHaveClass('chat-active')
    }

  const sentOnChat = async () => {
      await waitFor(() => expect(vi.mocked(streamFetch)).toHaveBeenCalled())
      const body = JSON.parse((vi.mocked(streamFetch).mock.calls[0][1] as { body: string }).body)
      expect(body.case_id).toBeUndefined()
      expect(document.querySelector('.soc-console')).toHaveClass('chat-active')
    }

    beforeEach(() => vi.mocked(streamFetch).mockClear())
    afterEach(() => {
      vi.mocked(streamFetch).mockClear()
      localStorage.clear() // Chat keeps its history and thread map here
    })

    it('sends to the drawer case and leaves the dock closed', async () => {
      renderConsole('/cases')
      fireEvent.click(await screen.findByText('Defense Evasion: Obfuscated Loader'))
      await screen.findByRole('dialog', { name: 'Case' })
      ask('why this loader?')
      await sentOnCase()
    })

    it('sends to the full-page case and leaves the dock closed', async () => {
      renderConsole('/cases?case=case-2026-0142')
      await screen.findByRole('tab', { name: /Summary/ })
      ask('why this loader?')
      await sentOnCase()
    })

    it('opens the dock with the text when no case is open', async () => {
      renderConsole('/cases')
      await screen.findByText('Defense Evasion: Obfuscated Loader')
      ask('why this loader?')
      await sentOnChat()
    })
  })

  it('opens the AI Decisions review queue', async () => {
    renderConsole()
    clickScreen('AI Decisions')
    fireEvent.click(await screen.findByText('Cluster merge'))
    expect(screen.getByText('AI recommendation')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /All decisions/ })).toBeInTheDocument()
  })

  it('switches Workflows tabs and loads a read-only skills list from the API', async () => {
    renderConsole()
    clickScreen('Agents & workflows')
    fireEvent.click(screen.getByRole('tab', { name: 'Agents' }))
    expect(screen.getByText(/Each can use its own model/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    expect(await screen.findByText('UI Demo Skill')).toBeInTheDocument()
    // Skills are files (#882 decision 7): no build/import/toggle/delete controls
    expect(screen.queryByRole('button', { name: /Build Skill|Import Zip/ })).toBeNull()
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByTitle('Delete skill')).toBeNull()
  })

  it('opens the chat dock without error', () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: /Ask Vigil/ }))
    expect(screen.getByText(/investigate a finding/)).toBeInTheDocument()
  })

  it('keeps the dock at 400px above 600px', () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
    renderConsole()
    expect(document.querySelector('.soc-console')).toHaveStyle({ '--chat-w': '400px' })
    fireEvent.click(screen.getByRole('button', { name: /Ask Vigil/ }))
    expect(screen.queryByRole('separator', { name: 'Resize Vigil Assistant' })).toBeNull()
    expect(localStorage.getItem('soc.chat.width.v1')).toBeNull()
  })

  it('uses the full viewport at 600px and under', () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 500 })
    renderConsole()
    expect(document.querySelector('.soc-console')).toHaveStyle({ '--chat-w': '500px' })
  })

  it('opens the dock on the current page without a per-chat model', () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: /Ask Vigil/ }))
    expect(screen.getByText('Private to you')).toBeInTheDocument()
    expect(screen.getByText('Using Dashboard')).toBeInTheDocument()
    expect(screen.queryByTitle('Chat settings')).toBeNull()
    expect(screen.queryByPlaceholderText(/Override default system prompt/)).toBeNull()
  })

  // without the count, a parked run's question sat in a tab nobody opened
  it('counts the runs waiting on someone in the nav row', async () => {
    vi.mocked(approvalsApi.listPending).mockResolvedValue({
      data: { actions: [{ action_id: 'a' }, { action_id: 'b' }] },
    } as never)

    renderConsole()

    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    expect(await screen.findByRole('button', { name: 'AI Decisions (2 waiting)' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'More' })).not.toHaveTextContent('2')
  })

  // The badge counts pending approvals, so the click has to land on the tab
  // holding them: opening the feedback tab instead showed "No decisions
  // awaiting feedback" while the counted questions sat one tab over (#746).
  it('opens the approvals tab when the nav badge is what was clicked', async () => {
    vi.mocked(approvalsApi.listPending).mockResolvedValue({
      data: {
        actions: [
          { action_id: 'a', action_type: 'isolate_host', title: 'isolate_host: host1', target: 'host1' },
          { action_id: 'b', action_type: 'block_ip', title: 'block_ip: 1.2.3.4', target: '1.2.3.4' },
        ],
      },
    } as never)

    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    fireEvent.click(await screen.findByRole('button', { name: 'AI Decisions (2 waiting)' }))

    const approvals = await screen.findByRole('tab', { name: 'Pending Approvals (2)' })
    expect(approvals).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: /^Pending \(/ })).toHaveAttribute('aria-selected', 'false')
    expect(screen.getByText('isolate_host: host1')).toBeInTheDocument()
    vi.mocked(approvalsApi.listPending).mockResolvedValue({ data: { actions: [] } } as never)
  })

  // …and an unbadged click keeps landing on the feedback queue, which is what
  // the screen is for when nothing is parked.
  it('opens the feedback tab when nothing is waiting on approval', async () => {
    renderConsole()
    clickScreen('AI Decisions')

    expect(await screen.findByRole('tab', { name: /^Pending \(/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Pending Approvals (0)' })).toHaveAttribute('aria-selected', 'false')
  })

  // The detail view returns before the tab list renders, so a badged click
  // while a decision was open used to change only which list the detail read
  // from -- losing the open decision and never showing the approvals queue.
  it('leaves an open decision detail when the badge sends it to approvals', async () => {
    vi.mocked(approvalsApi.listPending).mockResolvedValue({
      data: { actions: [{ action_id: 'a', action_type: 'isolate_host', title: 'isolate_host: host1', target: 'host1' }] },
    } as never)

    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    fireEvent.click(await screen.findByRole('button', { name: 'AI Decisions (1 waiting)' }))
    // open a decision from the feedback queue first
    fireEvent.click(screen.getByRole('tab', { name: /^Pending \(/ }))
    fireEvent.click(await screen.findByText('Cluster merge'))
    expect(screen.getByText('AI recommendation')).toBeInTheDocument()

    // now the badge: the approvals queue must actually appear
    clickScreen('AI Decisions (1 waiting)')

    expect(await screen.findByRole('tab', { name: 'Pending Approvals (1)' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('isolate_host: host1')).toBeInTheDocument()
    expect(screen.queryByText(/no longer in the current list/)).toBeNull()
    vi.mocked(approvalsApi.listPending).mockResolvedValue({ data: { actions: [] } } as never)
  })

  // Approving the last action empties the queue, so the nav row loses its badge
  // while the operator is still sitting on ?tab=approvals. The unbadged click
  // has to move them, which go()'s dedupe guard used to swallow.
  it('moves off the approvals tab when the badge is gone', async () => {
    renderConsole('/decisions?tab=approvals')

    expect(await screen.findByRole('tab', { name: 'Pending Approvals (0)' })).toHaveAttribute('aria-selected', 'true')
    clickScreen('AI Decisions')

    expect(await screen.findByRole('tab', { name: /^Pending \(/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('streams an assistant response through the chat SSE pipe', async () => {
    const chunks = [
      'data: {"type":"text","content":"Hello"}\n',
      'data: {"type":"text","content":" world"}\n',
    ].map((s) => new TextEncoder().encode(s))
    let i = 0
    vi.mocked(streamFetch).mockClear()
    vi.mocked(streamFetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: () =>
            i < chunks.length
              ? Promise.resolve({ done: false, value: chunks[i++] })
              : Promise.resolve({ done: true, value: undefined }),
        }),
      },
    } as unknown as Response)

    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: /Ask Vigil/ }))
    fireEvent.change(screen.getByPlaceholderText(/Ask Vigil/), { target: { value: 'hi' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    // waitFor re-queries each poll, so it settles on the final message node and
    // not the streaming bubble, which detaches when the stream completes
    await waitFor(() => expect(screen.getByText('Hello world')).toBeInTheDocument())
    expect(vi.mocked(streamFetch)).toHaveBeenCalledWith(
      '/claude/chat/stream',
      expect.objectContaining({ method: 'POST' }),
    )
    const body = JSON.parse(
      (vi.mocked(streamFetch).mock.calls[0][1] as { body: string }).body,
    )
    expect(body.page_context).toBe('dashboard')
    expect(body.model).toBeUndefined()
    expect(body.agent_id).toBeUndefined()
    expect(body.system_prompt).toBeUndefined()
    expect(body.max_tokens).toBeUndefined()
  })

  it('submits decision feedback through the inline review pane', async () => {
    renderConsole()
    clickScreen('AI Decisions')
    fireEvent.click(await screen.findByText('Cluster merge'))
    fireEvent.change(screen.getByPlaceholderText('Your name / analyst ID'), {
      target: { value: 'QA Analyst' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Approve/ }))
    expect(vi.mocked(aiDecisionsApi.submitFeedback)).toHaveBeenCalledWith(
      'd-4471',
      expect.objectContaining({ human_reviewer: 'QA Analyst', human_decision: 'agree' }),
    )
  })

  it('exports the visible timeline events as CSV', async () => {
    // jsdom has no object-URL plumbing; the stub also captures the Blob
    let captured: Blob | undefined
    const createUrl = vi.fn((b: Blob) => {
      captured = b
      return 'blob:mock'
    })
    ;(URL as unknown as { createObjectURL: unknown }).createObjectURL = createUrl
    ;(URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn()
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {})

    renderConsole()
    fireEvent.click(screen.getByRole('tab', { name: 'Timeline' }))
    await screen.findByText(/events$/)
    fireEvent.click(screen.getByTitle('Export visible events (CSV)'))

    expect(createUrl).toHaveBeenCalledTimes(1)
    expect(captured?.type).toBe('text/csv')
    clickSpy.mockRestore()
  })

  describe('Timeline date range', () => {
    const range = vi.mocked(timelineApi.getTimelineRange)
    const openTimeline = async () => {
      renderConsole()
      fireEvent.click(screen.getByRole('tab', { name: 'Timeline' }))
      await screen.findByText('2 events')
      range.mockClear()
    }
    const setDate = (name: string, value: string) =>
      fireEvent.change(screen.getByLabelText(name), { target: { value } })

    it('sends the selected calendar days as inclusive start and end, and Clear drops them', async () => {
      await openTimeline()
      setDate('Timeline start date', '2026-06-12')
      setDate('Timeline end date', '2026-06-12')

      expect(await screen.findByText('1 event')).toBeInTheDocument()
      expect(range).toHaveBeenLastCalledWith({
        limit: 200,
        start: new Date('2026-06-12T00:00:00').toISOString(),
        end: new Date('2026-06-12T23:59:59.999').toISOString(),
      })

      fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
      expect(await screen.findByText('2 events')).toBeInTheDocument()
      expect(range).toHaveBeenLastCalledWith({ limit: 200, start: undefined, end: undefined })
    })

    it('offers Retry when a reload fails instead of leaving the timeline blank', async () => {
      await openTimeline()
      range.mockRejectedValueOnce(new Error('boom'))
      setDate('Timeline start date', '2026-06-10')

      expect(await screen.findByText('Couldn’t load the timeline')).toBeInTheDocument()
      expect(screen.getByText('Couldn’t load')).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      expect(await screen.findByText('2 events')).toBeInTheDocument()
    })

    it('flags an inverted range and sends no request for it', async () => {
      await openTimeline()
      setDate('Timeline start date', '2026-06-13')
      setDate('Timeline end date', '2026-06-12')

      expect(await screen.findByRole('alert')).toHaveTextContent('Start date must be on or before end date.')
      expect(screen.getByLabelText('Timeline start date')).toHaveAttribute('aria-invalid', 'true')
      expect(screen.getByText('Invalid date range')).toBeInTheDocument()
      expect(range).not.toHaveBeenCalledWith(expect.objectContaining({ start: new Date('2026-06-13T00:00:00').toISOString(), end: new Date('2026-06-12T23:59:59.999').toISOString() }))
    })
  })

  it('shows Act, and Assist when force-manual is set or auto-response is off', async () => {
    const { unmount } = renderConsole()
    expect(await screen.findByText('Act · reversible changes on its own')).toBeInTheDocument()
    expect(screen.getByText('Autonomy', { selector: '.vg-autonomy-label' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'How autonomy is derived' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('force_manual_approval')
    expect(screen.getByRole('tooltip')).toHaveTextContent('auto_response_enabled')
    unmount()

    vi.mocked(configApi.getAutonomy).mockResolvedValueOnce({
      data: { auto_response_enabled: true, force_manual_approval: true },
    } as never)
    const forced = renderConsole()
    expect(await screen.findByText('Assist · asks before changes')).toBeInTheDocument()
    forced.unmount()

    vi.mocked(configApi.getAutonomy).mockResolvedValueOnce({
      data: { auto_response_enabled: false, force_manual_approval: false },
    } as never)
    renderConsole()
    expect(await screen.findByText('Assist · asks before changes')).toBeInTheDocument()
  })

  it('opens Limits & autonomy from the chip in both Assist and Act', async () => {
    const act = renderConsole()
    fireEvent.click(await screen.findByRole('button', { name: 'Autonomy · Act · reversible changes on its own' }))
    const where = screen.getByTestId('console-location')
    expect(where).toHaveAttribute('data-path', '/settings')
    expect(where).toHaveAttribute('data-search', '?section=autoinvestigate')
    act.unmount()

    vi.mocked(configApi.getAutonomy).mockResolvedValueOnce({
      data: { auto_response_enabled: true, force_manual_approval: true },
    } as never)
    renderConsole()
    fireEvent.click(await screen.findByRole('button', { name: 'Autonomy · Assist · asks before changes' }))
    expect(screen.getByTestId('console-location')).toHaveAttribute('data-search', '?section=autoinvestigate')
  })

  it('paints vg-dark and vg-light from the profile menu', async () => {
    const { container } = renderConsole()
    const root = container.querySelector('.soc-console')
    expect(root).toHaveClass('vg-dark')
    expect(root).toHaveAttribute('data-theme', 'dark')
    await screen.findByText('Act · reversible changes on its own')
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Switch to light mode' }))
    expect(root).toHaveClass('vg-light')
    expect(root).not.toHaveClass('vg-dark')
    expect(root).toHaveAttribute('data-theme', 'light')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Switch to dark mode' }))
    expect(root).toHaveClass('vg-dark')
  })

  it('reads Good when the checks are clear and Poor when health is not', async () => {
    const { unmount } = renderConsole()
    const good = await screen.findByRole('status', { name: 'System status' })
    expect(good).toHaveTextContent('No problems reported.')
    expect(good).toHaveAttribute('data-level', 'good')
    expect(good).not.toHaveClass('is-poor')
    unmount()

    vi.mocked(consoleApi.getHealth).mockResolvedValue({
      data: {
        status: 'degraded',
        storage: { database_available: true },
        schema: { state: 'current' },
      },
    } as never)
    renderConsole()
    const poor = await screen.findByRole('status', { name: 'System status' })
    expect(poor).toHaveAttribute('data-level', 'poor')
    expect(poor).toHaveTextContent('Health is degraded.')
    expect(poor).toHaveClass('is-poor')
  })

  it('re-reads the status sources every 30 seconds and turns Poor when one degrades later', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      renderConsole()
      expect(await screen.findByRole('status', { name: 'System status' })).toHaveAttribute('data-level', 'good')
      vi.mocked(consoleApi.getHealth).mockResolvedValue({ data: { status: 'degraded' } } as never)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000)
      })
      const line = screen.getByRole('status', { name: 'System status' })
      expect(line).toHaveClass('is-poor')
      expect(line).toHaveTextContent('Health is degraded.')
    } finally {
      vi.useRealTimers()
    }
  })

  it('omits a failed routability read instead of calling the line Fair', async () => {
    vi.mocked(consoleApi.getRoutability).mockRejectedValueOnce(new Error('403'))
    renderConsole()
    const line = await screen.findByRole('status', { name: 'System status' })
    expect(line).toHaveAttribute('data-level', 'good')
    expect(line).not.toHaveTextContent('No routable provider.')
  })

  it('exports the filtered findings as a browser CSV download', async () => {
    let captured: Blob | undefined
    const createUrl = vi.fn((blob: Blob) => {
      captured = blob
      return 'blob:findings'
    })
    ;(URL as unknown as { createObjectURL: unknown }).createObjectURL = createUrl
    ;(URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn()
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    renderConsole()
    await screen.findByText('f-20260614-3b5c585e')
    fireEvent.click(screen.getByTitle('Export filtered findings as CSV'))

    expect(createUrl).toHaveBeenCalledTimes(1)
    expect(captured?.type).toBe('text/csv;charset=utf-8')
    clickSpy.mockRestore()
  })

  describe('console tour', () => {
    const NAV_TITLE = 'Pages are grouped by what you are doing'
    const ATTENTION_TITLE = 'Decisions wait here'
    const ASK_TITLE = 'Ask Vigil anywhere'
    let rectSpy: { mockRestore: () => void }

    beforeEach(() => {
      localStorage.removeItem(CONSOLE_TOUR_SEEN_KEY)
      rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
        const label = this.getAttribute('aria-label')
        if (label === 'Primary') return domRect(40, 10, 500, 46)
        if (label === 'Needs your attention') return domRect(200, 24, 640, 180)
        if (this.classList.contains('chat-fab')) return domRect(620, 800, 148, 44)
        return domRect(0, 0, 0, 0)
      })
    })

    afterEach(() => {
      rectSpy.mockRestore()
    })

    it('points at the primary nav until Skip, then stays hidden on reload', () => {
      const first = renderConsole()
      const ring = document.querySelector('.console-tour-ring')
      expect(screen.getByRole('dialog', { name: NAV_TITLE })).toHaveTextContent('Watch intake (Overview, Triage)')
      expect(ring).toHaveAttribute('data-stop', 'nav')
      expect(document.querySelector('.console-tour-step')?.textContent).toBe('Step 1 of 3')
      expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument()
      expect(ring).toHaveStyle({ top: '34px', left: '4px', width: '512px', height: '58px' })
      expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Skip tour' }))
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(localStorage.getItem(CONSOLE_TOUR_SEEN_KEY)).toBe('1')

      first.unmount()
      renderConsole()
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    })

    it('walks Home then Ask Vigil, and Done writes the seen flag', async () => {
      renderConsole('/cases')
      fireEvent.click(screen.getByRole('button', { name: 'Next' }))
      expect(screen.getByTestId('console-location')).toHaveAttribute('data-path', '/home')
      expect(await screen.findByRole('dialog', { name: ATTENTION_TITLE })).toBeInTheDocument()
      const attention = document.querySelector('.console-tour-ring')
      expect(attention).toHaveAttribute('data-stop', 'attention')
      expect(attention).toHaveStyle({ top: '194px', left: '18px' })
      expect(screen.getByRole('dialog', { name: ATTENTION_TITLE })).toHaveTextContent('Approve or reject here, or open the case.')
      expect(document.querySelector('.console-tour-step')?.textContent).toBe('Step 2 of 3')
      expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Next' }))
      expect(screen.getByRole('dialog', { name: ASK_TITLE })).toHaveTextContent('Conversations are private to you and kept in your history.')
      const ask = document.querySelector('.console-tour-ring')
      expect(ask).toHaveAttribute('data-stop', 'ask')
      expect(ask).toHaveStyle({ top: '614px', left: '794px' })
      expect(screen.getByRole('button', { name: 'Ask Vigil chat assistant' })).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Done' }))
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(localStorage.getItem(CONSOLE_TOUR_SEEN_KEY)).toBe('1')
    })

    it('closes the dock and leaves wall mode so the stop target is mounted', async () => {
      renderConsole('/overview')
      await screen.findByRole('button', { name: 'Full screen' })
      fireEvent.click(screen.getByRole('button', { name: 'Ask Vigil chat assistant' }))
      expect(screen.queryByRole('button', { name: 'Ask Vigil chat assistant' })).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))

      fireEvent.click(screen.getByRole('button', { name: 'Next' }))
      expect(await screen.findByRole('dialog', { name: ATTENTION_TITLE })).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Next' }))
      expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Ask Vigil chat assistant' })).toBeInTheDocument()
      expect(document.querySelector('.console-tour-ring')).toHaveAttribute('data-stop', 'ask')
    })

    it('skips Home for an operator who cannot open it', () => {
      authState.allow = (permission: string) => permission !== 'ai_decisions.approve'
      renderConsole('/dashboard')
      expect(screen.getByRole('dialog', { name: NAV_TITLE })).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Next' }))
      expect(screen.getByRole('dialog', { name: ASK_TITLE })).toBeInTheDocument()
      expect(document.querySelector('.console-tour-step')?.textContent).toBe('Step 2 of 2')
      expect(screen.getByTestId('console-location')).toHaveAttribute('data-path', '/dashboard')
      expect(screen.queryByText(/Access denied/)).not.toBeInTheDocument()
      expect(screen.queryByRole('dialog', { name: ATTENTION_TITLE })).not.toBeInTheDocument()
    })

    it('starts again at the primary nav from the account menu', () => {
      localStorage.setItem(CONSOLE_TOUR_SEEN_KEY, '1')
      renderConsole()
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
      fireEvent.click(screen.getByRole('menuitem', { name: 'Take the tour' }))
      expect(screen.getByRole('dialog', { name: NAV_TITLE })).toBeInTheDocument()
      expect(screen.queryByRole('menu', { name: 'Account' })).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Skip tour' }))
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(localStorage.getItem(CONSOLE_TOUR_SEEN_KEY)).toBe('1')
    })
  })
})
