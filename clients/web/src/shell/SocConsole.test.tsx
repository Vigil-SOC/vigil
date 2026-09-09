import { afterEach, describe, it, expect, beforeAll, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { ColorSchemeProvider } from '../contexts/ColorSchemeContext'
import SocConsole from './SocConsole'
// these resolve to the mocked implementations (vi.mock below is hoisted)
import { streamFetch, aiDecisionsApi, approvalsApi } from '../services/api'

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { full_name: 'Test User', email: 'test@vigil.local', role_id: 'role-admin', mfa_enabled: false },
    logout: vi.fn(),
    hasPermission: () => true,
  }),
}))

// The theme provider bridges ColorSchemeContext, so it needs a real one above it.
function renderConsole(path = '/dashboard') {
  return render(
    <ColorSchemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/:screen" element={<SocConsole />} />
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
    getStatuses: () => Promise.resolve({ data: { statuses: [{ status: 'ok' }, { status: 'ok' }] } }),
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
            { id: 'incident-response', name: 'Incident Response', description: 'Respond to active incidents.', agents: ['triage', 'responder'], trigger_examples: ['"Run incident response"'] },
          ],
        },
      }),
  },
  attackApi: {
    getTechniqueRollup: () =>
      Promise.resolve({
        data: { techniques: [{ technique_id: 'T1567.002', count: 10, severities: { critical: 2, high: 3, medium: 4, low: 1 } }] },
      }),
    getFindingsByTechnique: () => Promise.resolve({ data: { findings: [] } }),
  },
  timelineApi: {
    getTimelineRange: () =>
      Promise.resolve({
        data: { events: [{ id: 'finding-f-1', start: '2026-06-12T11:36:33Z', type: 'finding', severity: 'medium', metadata: { finding_id: 'f-1' } }] },
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
    approve: vi.fn(() => Promise.resolve({})),
    reject: vi.fn(() => Promise.resolve({})),
  },
  configApi: {
    getTheme: () => Promise.resolve({ data: { theme: 'dark' } }),
    setTheme: () => Promise.resolve({ data: {} }),
    getIntegrations: () => Promise.resolve({ data: { enabled_integrations: [] } }),
    getGeneral: () => Promise.resolve({ data: { show_notifications: false } }),
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
    update: () => Promise.resolve({}),
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
  localStorage.clear()
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: defaultViewportWidth })
})

const title = () => screen.getByRole('heading', { level: 1 }).textContent

describe('SocConsole', () => {
  it('mounts on the Dashboard', () => {
    renderConsole()
    expect(title()).toBe('Dashboard')
    expect(screen.getByText('Security operations overview')).toBeInTheDocument()
  })

  it('renders the 404 screen for an unknown path and routes home', async () => {
    renderConsole('/does-not-exist')
    expect(title()).toBe('Page not found')
    // an unknown path is probed as a page extension first, so the 404 body only
    // lands once that resolution settles
    expect(await screen.findByText('404')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Back to dashboard/ }))
    expect(title()).toBe('Dashboard')
  })

  it('navigates across every screen via the nav rail', () => {
    renderConsole()
    const screens: [string, string][] = [
      ['Cases', 'Cases'],
      ['Case Metrics', 'Case Metrics'],
      ['Analytics', 'Analytics Dashboard'],
      ['AI Decisions', 'AI Decisions'],
      ['Workflows & Skills', 'Workflows & Skills'],
      ['Dashboard', 'Dashboard'],
    ]
    for (const [navLabel, pageTitle] of screens) {
      fireEvent.click(screen.getByRole('button', { name: navLabel }))
      expect(title()).toBe(pageTitle)
    }
  })

  it('switches the available Dashboard tabs and hides VStrike when disabled', async () => {
    renderConsole()
    fireEvent.click(screen.getByRole('tab', { name: 'ATT&CK' }))
    expect(screen.getByText(/Techniques by occurrence/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Timeline' }))
    expect(await screen.findByText(/events$/)).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'VStrike' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /VStrike/ })).not.toBeInTheDocument()
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
    fireEvent.click(screen.getByRole('button', { name: 'Cases' }))
    fireEvent.click(await screen.findByText('Defense Evasion: Obfuscated Loader'))
    const back = screen.getByRole('button', { name: /All cases/ })
    expect(back).toBeInTheDocument()
    // "Case details" is stable Overview content; "Linked findings" moved to the
    // Investigation tab
    expect(screen.getByText('Case details')).toBeInTheDocument()
    fireEvent.click(back)
    expect(screen.getByRole('button', { name: 'New Case' })).toBeInTheDocument()
  })

  it('opens the AI Decisions review queue', async () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: 'AI Decisions' }))
    fireEvent.click(await screen.findByText('Cluster merge'))
    expect(screen.getByText('AI recommendation')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /All decisions/ })).toBeInTheDocument()
  })

  it('switches Workflows tabs and loads skills from the API', async () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: 'Workflows & Skills' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Agents' }))
    expect(screen.getByText('SOC Agents')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    expect(await screen.findByText('UI Demo Skill')).toBeInTheDocument()
  })

  it('opens the chat dock without error', () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: /Ask Vigil/ }))
    expect(screen.getByText(/investigate a finding/)).toBeInTheDocument()
  })

  it('restores and persists the preferred chat width', () => {
    localStorage.setItem('soc.chat.width.v1', '500')
    // jsdom's 1024 default would cap the dock at half the screen (512), which is
    // the next test's subject
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: /Ask Vigil/ }))

    const separator = screen.getByRole('separator', { name: 'Resize Vigil Assistant' })
    expect(separator).toHaveAttribute('aria-valuenow', '500')

    fireEvent.keyDown(separator, { key: 'ArrowLeft' })
    expect(separator).toHaveAttribute('aria-valuenow', '516')
    expect(localStorage.getItem('soc.chat.width.v1')).toBe('516')
  })

  it('uses the full viewport and disables resizing on narrow screens', () => {
    localStorage.setItem('soc.chat.width.v1', '700')
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 500 })
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: /Ask Vigil/ }))

    const separator = screen.getByRole('separator', { name: 'Resize Vigil Assistant' })
    expect(separator).toHaveAttribute('aria-valuemin', '500')
    expect(separator).toHaveAttribute('aria-valuemax', '500')
    expect(separator).toHaveAttribute('aria-valuenow', '500')
    expect(separator).toHaveAttribute('tabindex', '-1')
    expect(localStorage.getItem('soc.chat.width.v1')).toBe('700')
  })

  it('applies an accent + light mode from the Appearance settings page', () => {
    renderConsole('/settings')
    fireEvent.click(screen.getByRole('button', { name: 'Appearance' }))
    const cyan = screen.getByRole('button', { name: 'accent cyan' })
    fireEvent.click(cyan)
    expect(cyan).toHaveAttribute('aria-pressed', 'true')
    const light = screen.getByRole('button', { name: 'Light' })
    fireEvent.click(light)
    expect(light).toHaveAttribute('aria-pressed', 'true')
  })

  it('opens chat settings showing status, model and advanced sections', async () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: /Ask Vigil/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Chat settings' }))
    expect(await screen.findByText('2/2')).toBeInTheDocument()
    expect(screen.getByText(/Context ~/)).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/Override default system prompt/)).toBeInTheDocument()
    // extended thinking went with the harness move: one provider schema through
    // Bifrost has no thinking blocks, so the control would do nothing
    expect(screen.queryByRole('switch', { name: 'Extended thinking' })).toBeNull()
  })

  // without the count, a parked run's question sat in a tab nobody opened
  it('counts the runs waiting on someone in the rail', async () => {
    vi.mocked(approvalsApi.listPending).mockResolvedValue({
      data: { actions: [{ action_id: 'a' }, { action_id: 'b' }] },
    } as never)

    renderConsole()

    expect(await screen.findByRole('button', { name: 'AI Decisions (2 waiting)' })).toBeInTheDocument()
    vi.mocked(approvalsApi.listPending).mockResolvedValue({ data: { actions: [] } } as never)
  })

  // The badge counts pending approvals, so the click has to land on the tab
  // holding them: opening the feedback tab instead showed "No decisions
  // awaiting feedback" while the counted questions sat one tab over (#746).
  it('opens the approvals tab when the rail badge is what was clicked', async () => {
    vi.mocked(approvalsApi.listPending).mockResolvedValue({
      data: {
        actions: [
          { action_id: 'a', action_type: 'isolate_host', title: 'isolate_host: host1', target: 'host1' },
          { action_id: 'b', action_type: 'block_ip', title: 'block_ip: 1.2.3.4', target: '1.2.3.4' },
        ],
      },
    } as never)

    renderConsole()
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
    fireEvent.click(screen.getByRole('button', { name: 'AI Decisions' }))

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
    fireEvent.click(await screen.findByRole('button', { name: 'AI Decisions (1 waiting)' }))
    // open a decision from the feedback queue first
    fireEvent.click(screen.getByRole('tab', { name: /^Pending \(/ }))
    fireEvent.click(await screen.findByText('Cluster merge'))
    expect(screen.getByText('AI recommendation')).toBeInTheDocument()

    // now the badge: the approvals queue must actually appear
    fireEvent.click(screen.getByRole('button', { name: 'AI Decisions (1 waiting)' }))

    expect(await screen.findByRole('tab', { name: 'Pending Approvals (1)' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('isolate_host: host1')).toBeInTheDocument()
    expect(screen.queryByText(/no longer in the current list/)).toBeNull()
    vi.mocked(approvalsApi.listPending).mockResolvedValue({ data: { actions: [] } } as never)
  })

  // Approving the last action empties the queue, so the rail loses its badge
  // while the operator is still sitting on ?tab=approvals. The unbadged click
  // has to move them, which go()'s dedupe guard used to swallow.
  it('moves off the approvals tab when the badge is gone', async () => {
    renderConsole('/decisions?tab=approvals')

    expect(await screen.findByRole('tab', { name: 'Pending Approvals (0)' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(screen.getByRole('button', { name: 'AI Decisions' }))

    expect(await screen.findByRole('tab', { name: /^Pending \(/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('streams an assistant response through the chat SSE pipe', async () => {
    const chunks = [
      'data: {"type":"text","content":"Hello"}\n',
      'data: {"type":"text","content":" world"}\n',
    ].map((s) => new TextEncoder().encode(s))
    let i = 0
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
  })

  it('submits decision feedback through the inline review pane', async () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: 'AI Decisions' }))
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
})
