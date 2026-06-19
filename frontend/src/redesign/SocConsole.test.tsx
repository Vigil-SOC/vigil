/**
 * Smoke test for the SOC Console redesign preview.
 * Verifies the shell mounts and every screen / dashboard tab / master-detail
 * flow renders without throwing (catches runtime errors tsc can't see).
 */
import { describe, it, expect, beforeAll, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import SocConsole from './SocConsole'

// SocConsole is URL-driven (each screen owns /redesign/<screen>, cases deep-link
// to /redesign/cases?case=<caseId>), so mount it inside a router with that route.
function renderConsole(path = '/redesign/dashboard') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/redesign/:screen" element={<SocConsole />} />
      </Routes>
    </MemoryRouter>,
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
    submitFeedback: () => Promise.resolve({}),
  },
  approvalsApi: {
    listPending: () => Promise.resolve({ data: { actions: [] } }),
    approve: () => Promise.resolve({}),
    reject: () => Promise.resolve({}),
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
    renderConsole('/redesign/does-not-exist')
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
    // ATT&CK
    fireEvent.click(screen.getByRole('button', { name: 'ATT&CK' }))
    expect(screen.getByText(/Techniques by occurrence/)).toBeInTheDocument()
    // Timeline (exercises ResizeObserver + layout math); count resolves async
    fireEvent.click(screen.getByRole('button', { name: 'Timeline' }))
    expect(await screen.findByText(/events$/)).toBeInTheDocument()
    // Entity Graph stub — "Entity Graph" is both a (inert) rail item and a
    // dashboard tab, so target the tab button specifically.
    const entityTab = screen
      .getAllByRole('button', { name: 'Entity Graph' })
      .find((b) => b.classList.contains('tab'))!
    fireEvent.click(entityTab)
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
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }))
    expect(screen.getByText('SOC Agents')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Skills' }))
    // skills load asynchronously from the mocked skills client
    expect(await screen.findByText('UI Demo Skill')).toBeInTheDocument()
  })

  it('opens the chat dock and applies an accent tweak without error', () => {
    renderConsole()
    fireEvent.click(screen.getByRole('button', { name: /Ask Vigil/ }))
    // wired chat starts empty with its prompt
    expect(screen.getByText(/investigate a finding/)).toBeInTheDocument()
    // open tweaks and pick the cyan accent
    fireEvent.click(screen.getByRole('button', { name: 'Theme tweaks' }))
    fireEvent.click(screen.getByRole('button', { name: 'accent cyan' }))
    // density toggle
    fireEvent.click(screen.getByRole('button', { name: 'Comfortable' }))
    expect(screen.getByRole('button', { name: 'Compact' })).toBeInTheDocument()
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
})
