/**
 * Smoke test for the SOC Console redesign preview.
 * Verifies the shell mounts and every screen / dashboard tab / master-detail
 * flow renders without throwing (catches runtime errors tsc can't see).
 */
import { describe, it, expect, beforeAll, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { ThemeProvider } from '../contexts/ThemeContext'
import SocConsole from './SocConsole'
// these resolve to the mocked implementations (vi.mock below is hoisted)
import { streamFetch, aiDecisionsApi } from '../services/api'

// SocConsole reads the session from AuthContext (user menu + permission-gated
// nav). Stub it with a full-permission user so every rail item + screen renders.
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { full_name: 'Test User', email: 'test@vigil.local', role_id: 'role-admin', mfa_enabled: false },
    logout: vi.fn(),
    hasPermission: () => true,
  }),
}))

// SocConsole is URL-driven (each screen owns /<screen>, cases deep-link to
// /cases?case=<caseId>), so mount it inside a router with that route.
// SocConsole's theme provider bridges the app-wide ThemeContext (mode is
// backend-persisted), so it must mount inside a real ThemeProvider — same as
// production (main.tsx wraps the whole app).
function renderConsole(path = '/dashboard') {
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/:screen" element={<SocConsole />} />
        </Routes>
      </MemoryRouter>
    </ThemeProvider>,
  )
}

// The Cases screen is wired to the backend; mock the shared api client so the
// fetch resolves deterministically in jsdom (no network). Chat's agent roster
// comes from the same module. Literals are inlined — vi.mock is hoisted.
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
  // chat settings panel: model list + MCP tool status
  claudeApi: {
    getModels: () => Promise.resolve({ data: { models: [{ id: 'claude-sonnet-4-6', name: 'Claude Sonnet 4.6' }] } }),
  },
  mcpApi: {
    getStatuses: () => Promise.resolve({ data: { statuses: [{ status: 'ok' }, { status: 'ok' }] } }),
  },
  // chat resolves its default model from the chat_default component assignment
  aiConfigApi: {
    getConfig: () => Promise.resolve({ data: { components: [], assignments: {} } }),
  },
  // chat streaming helper — a vi.fn so the SSE test can supply a streaming body
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
  // AI Decisions screen — Pending tab (getPendingFeedback) + stats KPIs.
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
    listPending: () => Promise.resolve({ data: { actions: [] } }),
    approve: vi.fn(() => Promise.resolve({})),
    reject: vi.fn(() => Promise.resolve({})),
  },
  // ThemeContext loads/saves the light/dark preference on mount + on toggle;
  // the shell also reads integrations (nav membership) + general settings
  // (desktop-notification gating) on mount.
  configApi: {
    getTheme: () => Promise.resolve({ data: { theme: 'dark' } }),
    setTheme: () => Promise.resolve({ data: {} }),
    getIntegrations: () => Promise.resolve({ data: { enabled_integrations: [] } }),
    getGeneral: () => Promise.resolve({ data: { show_notifications: false } }),
  },
  // nav membership poll (Auto Ops gating plumbing)
  orchestratorApi: {
    getStatus: () => Promise.resolve({ data: { enabled: false } }),
  },
  // chat cost band (debounced pre-call estimate) + reasoning-trace clients
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

// Skills tab fetches via the dedicated skills client (not the shared api).
vi.mock('../services/skillsApi', () => ({
  skillsApi: {
    list: () =>
      Promise.resolve([
        { skill_id: 's-1', name: 'UI Demo Skill', description: 'Demo skill.', category: 'custom', version: 1, is_active: true },
      ]),
    update: () => Promise.resolve({}),
  },
}))

// jsdom lacks ResizeObserver, which the interactive Timeline uses.
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

const title = () => screen.getByRole('heading', { level: 1 }).textContent

describe('SocConsole redesign', () => {
  it('mounts on the Dashboard', () => {
    renderConsole()
    expect(title()).toBe('Dashboard')
    expect(screen.getByText('Security operations overview')).toBeInTheDocument()
  })

  it('renders the 404 screen for an unknown path and routes home', () => {
    renderConsole('/does-not-exist')
    expect(title()).toBe('Page not found')
    expect(screen.getByText('404')).toBeInTheDocument()
    // the in-shell "Back to dashboard" action returns to a real screen
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

  it('switches every Dashboard tab including the interactive Timeline', async () => {
    renderConsole()
    // ATT&CK (the dashboard tabs carry role=tab for screen readers)
    fireEvent.click(screen.getByRole('tab', { name: 'ATT&CK' }))
    expect(screen.getByText(/Techniques by occurrence/)).toBeInTheDocument()
    // Timeline (exercises ResizeObserver + layout math); count resolves async
    fireEvent.click(screen.getByRole('tab', { name: 'Timeline' }))
    expect(await screen.findByText(/events$/)).toBeInTheDocument()
    // Entity Graph stub
    fireEvent.click(screen.getByRole('tab', { name: 'Entity Graph' }))
    expect(screen.getByText('Coming soon.', { exact: false })).toBeInTheDocument()
  })

  it('opens the Cases master-detail and returns to the table', async () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: 'Cases' }))
    // wait for the wired fetch to populate the table, then click the first row
    fireEvent.click(await screen.findByText('Defense Evasion: Obfuscated Loader'))
    // detail view shows the back button + the Overview tab's content
    const back = screen.getByRole('button', { name: /All cases/ })
    expect(back).toBeInTheDocument()
    // detail opens on the Overview tab; "Case details" is its stable content
    // ("Linked findings" lives on the Investigation tab in the tabbed layout)
    expect(screen.getByText('Case details')).toBeInTheDocument()
    fireEvent.click(back)
    // back to the full table
    expect(screen.getByRole('button', { name: 'New Case' })).toBeInTheDocument()
  })

  it('opens the AI Decisions review queue', async () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: 'AI Decisions' }))
    // Pending tab loads from the mocked aiDecisionsApi; wait for the row
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
    // skills load asynchronously from the mocked skills client
    expect(await screen.findByText('UI Demo Skill')).toBeInTheDocument()
  })

  it('opens the chat dock without error', () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: /Ask Vigil/ }))
    // wired chat starts empty with its prompt
    expect(screen.getByText(/investigate a finding/)).toBeInTheDocument()
  })

  it('applies an accent + light mode from the Appearance settings page', () => {
    renderConsole('/settings')
    // Settings opens on the Appearance section (first nav item)
    fireEvent.click(screen.getByRole('button', { name: 'Appearance' }))
    // pick the cyan accent preset
    const cyan = screen.getByRole('button', { name: 'accent cyan' })
    fireEvent.click(cyan)
    expect(cyan).toHaveAttribute('aria-pressed', 'true')
    // switch to light mode; the toggle reflects the new state
    const light = screen.getByRole('button', { name: 'Light' })
    fireEvent.click(light)
    expect(light).toHaveAttribute('aria-pressed', 'true')
  })

  it('opens chat settings showing status, model and advanced sections', async () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: /Ask Vigil/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Chat settings' }))
    // MCP status resolves from the mocked api (2 of 2 servers ok)
    expect(await screen.findByText('2/2')).toBeInTheDocument()
    expect(screen.getByText(/Context ~/)).toBeInTheDocument()
    // extended-thinking switch + system-prompt override
    expect(screen.getByRole('switch', { name: 'Extended thinking' })).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/Override default system prompt/)).toBeInTheDocument()
  })

  it('streams an assistant response through the chat SSE pipe', async () => {
    // a Response-like object whose body yields two SSE text deltas then ends
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

    // the two text deltas are concatenated and rendered as the reply. waitFor
    // re-queries each poll so it settles on the final message node rather than
    // the transient streaming bubble (which detaches when the stream completes).
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
    // a reviewer name is required before the verdict buttons submit
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
    // jsdom has no object-URL plumbing — stub it so the export can run, and
    // capture the Blob it's handed to assert the CSV mime type
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
})
