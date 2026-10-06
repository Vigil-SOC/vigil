import { useState, type ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import CommandBar from './CommandBar'
import CaseDrawer from './CaseDrawer'
import type { BoardLink } from './commandBar'

const { execute, getCase, getFinding, getIntegrations, apiGet } = vi.hoisted(() => ({
  execute: vi.fn((..._args: unknown[]) => Promise.resolve({ data: {} })),
  getCase: vi.fn(),
  getFinding: vi.fn(),
  getIntegrations: vi.fn(),
  apiGet: vi.fn(),
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 'user-1' },
    hasPermission: () => true,
  }),
}))

vi.mock('../services/api', () => ({
  casesApi: {
    getById: (id: string) => getCase(id),
    getAll: () => Promise.resolve({
      data: {
        cases: [
          { case_id: 'case-9', title: 'Exact case', status: 'open', priority: 'high', assignee: 'ada', finding_ids: [], created_at: '2026-06-15T09:14:00Z' },
        ],
      },
    }),
    getSLA: () => Promise.resolve({ data: {} }),
    getRecord: () => Promise.resolve({ data: { rows: [], run_id: null, investigation_id: null } }),
    getComments: () => Promise.resolve({ data: { comments: [] } }),
    getTasks: () => Promise.resolve({ data: { tasks: [] } }),
    getEvidence: () => Promise.resolve({ data: { evidence: [] } }),
    getIOCs: () => Promise.resolve({ data: { iocs: [] } }),
    getEscalations: () => Promise.resolve({ data: { escalations: [] } }),
  },
  findingsApi: {
    getById: (id: string) => getFinding(id),
  },
  configApi: {
    getIntegrations: () => getIntegrations(),
  },
  timelineApi: {},
  caseSearchApi: { search: vi.fn() },
  agentsApi: { listAgents: vi.fn(() => Promise.resolve({ data: { agents: [] } })) },
  conversationsApi: {
    list: vi.fn(() => Promise.resolve({ data: { conversations: [] } })),
    get: vi.fn(() => Promise.resolve({ data: { messages: [] } })),
    update: vi.fn(() => Promise.resolve({ data: {} })),
    delete: vi.fn(),
    importHistory: vi.fn(() => Promise.resolve({ data: {} })),
  },
  reasoningApi: {
    getSessionSummary: vi.fn(() => Promise.resolve(null)),
    listInteractions: vi.fn(() => Promise.resolve({ interactions: [] })),
    getInteraction: vi.fn(),
  },
  streamFetch: vi.fn(),
  workflowApi: {
    execute: (id: string, params: unknown) => execute(id, params),
    getRun: vi.fn(() => Promise.resolve({ data: {} })),
  },
  approvalsApi: {
    needsYou: vi.fn(() => Promise.resolve({ data: { count: 0, items: [] } })),
    approve: vi.fn(() => Promise.resolve({})),
    reject: vi.fn(() => Promise.resolve({})),
  },
  default: {
    get: (path: string, config?: unknown) => apiGet(path, config),
    post: vi.fn(),
  },
}))

const BOARDS: BoardLink[] = [
  { key: 'cases', label: 'Cases' },
  { key: 'workflows', label: 'Agents & workflows' },
  { key: 'settings', label: 'Settings' },
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'metrics', label: 'Case Metrics' },
]

const RECENTS = 'vigil.command.recents.user-1'

function renderBar(props?: Partial<ComponentProps<typeof CommandBar>>) {
  const onOpenChat = props?.onOpenChat ?? vi.fn()
  const onOpenCase = props?.onOpenCase ?? vi.fn()
  const onGo = props?.onGo ?? vi.fn()
  render(<CommandBar boards={BOARDS} onOpenChat={onOpenChat} onOpenCase={onOpenCase} onGo={onGo} />)
  return { onOpenChat, onOpenCase, onGo }
}

function Shell() {
  const [caseId, setCaseId] = useState<string | null>(null)
  const location = useLocation()
  return (
    <>
      <div data-testid="where">{location.pathname}{location.search}</div>
      <div>Dashboard page</div>
      <CommandBar boards={BOARDS} onOpenChat={vi.fn()} onOpenCase={setCaseId} onGo={vi.fn()} />
      {caseId && (
        <CaseDrawer caseId={caseId} onClose={() => setCaseId(null)} pageKey="dashboard" />
      )}
    </>
  )
}

beforeEach(() => {
  localStorage.clear()
  execute.mockClear()
  execute.mockResolvedValue({ data: {} })
  getIntegrations.mockResolvedValue({ data: { enabled_integrations: [], integrations: {}, secrets_set: {} } })
  getCase.mockImplementation((id: string) => {
    if (id === 'case') return Promise.resolve({ data: { case_id: 'case', title: 'Exact case', finding_ids: [] } })
    if (id === 'case-9') {
      return Promise.resolve({
        data: { case_id: 'case-9', title: 'Exact case', status: 'open', priority: 'high', assignee: 'ada', finding_ids: [], created_at: '2026-06-15T09:14:00Z' },
      })
    }
    return Promise.reject(new Error('missing case'))
  })
  getFinding.mockImplementation((id: string) => (
    id === 'f-1'
      ? Promise.resolve({ data: { finding_id: 'f-1', title: 'LSASS' } })
      : Promise.reject(new Error('missing finding'))
  ))
  apiGet.mockImplementation((path: string) => {
    if (path === '/cases/search/full-text') {
      return Promise.resolve({
        data: { cases: [{ case_id: 'case-2', title: 'Loader in mail' }], comments: [], evidence: [] },
      })
    }
    if (path === '/cases/search/by-ioc') {
      return Promise.resolve({ data: { cases: [{ case_id: 'case-3', title: 'Beacon host' }] } })
    }
    return Promise.reject(new Error(path))
  })
})

afterEach(() => {
  localStorage.clear()
})

describe('CommandBar', () => {
  it('opens with ⌘K, and Esc and Tab write no recent search', () => {
    const { onOpenChat } = renderBar()
    fireEvent.keyDown(window, { key: 'k', metaKey: true })
    const input = screen.getByRole('combobox', { name: 'Find a case, ask Vigil, or run a command' })
    expect(input).toHaveFocus()
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    fireEvent.change(input, { target: { value: 'why this beacon' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(localStorage.getItem(RECENTS)).toBeNull()

    fireEvent.change(input, { target: { value: 'why this beacon' } })
    fireEvent.keyDown(input, { key: 'Tab' })
    expect(onOpenChat).toHaveBeenCalledWith('why this beacon')
    expect(localStorage.getItem(RECENTS)).toBeNull()
  })

  it('ranks an exact id, then full-text and ioc hits, then boards', async () => {
    const { onOpenCase } = renderBar()
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: 'case' } })
    await waitFor(() => {
      expect(screen.getAllByRole('option').map((option) => option.querySelector('.vg-command-label')?.textContent)).toEqual([
        'Exact case',
        'Loader in mail',
        'Beacon host',
        'Cases',
        'Case Metrics',
      ])
    })
    expect(screen.getAllByRole('option').map((option) => option.querySelector('.vg-command-dest')?.textContent)).toEqual([
      'Case',
      'Case',
      'Case',
      'Page',
      'Page',
    ])
    expect(getCase).toHaveBeenCalledWith('case')
    expect(getFinding).toHaveBeenCalledWith('case')
    expect(apiGet).toHaveBeenCalledWith('/cases/search/full-text', { params: { query: 'case' } })
    expect(apiGet).toHaveBeenCalledWith('/cases/search/by-ioc', { params: { ioc_value: 'case' } })

    fireEvent.click(screen.getByRole('option', { name: /Loader in mail/ }))
    expect(onOpenCase).toHaveBeenCalledWith('case-2')
    expect(JSON.parse(localStorage.getItem(RECENTS) || '[]')).toEqual(['case'])

    getCase.mockImplementation(() => new Promise(() => {}))
    apiGet.mockImplementation(() => new Promise(() => {}))
    fireEvent.change(input, { target: { value: 'other' } })
    expect(screen.queryByRole('option', { name: /Exact case/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Loader in mail/ })).not.toBeInTheDocument()
  })

  it('shows recents and the five live commands when the query is empty', () => {
    localStorage.setItem(RECENTS, JSON.stringify(['beacon']))
    renderBar()
    fireEvent.focus(screen.getByRole('combobox'))
    expect(screen.getAllByRole('option').map((option) => option.querySelector('.vg-command-label')?.textContent)).toEqual([
      'beacon',
      '/investigate',
      '/hunt',
      '/replay',
      '/ask',
      '/ticket',
    ])
    expect(screen.queryByRole('option', { name: /\/hold/ })).not.toBeInTheDocument()
  })

  it('skips later commands and stops Enter on the preview before execute', async () => {
    const { onOpenChat } = renderBar()
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '/' } })
    expect(screen.getByRole('option', { name: /\/hold/ })).toBeDisabled()
    expect(screen.getByRole('option', { name: /Custom commands/ })).toBeDisabled()
    for (let step = 0; step < 4; step += 1) fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(screen.getByRole('option', { selected: true })).toHaveTextContent('/ticket')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(screen.getByRole('option', { selected: true })).toHaveTextContent('/investigate')

    fireEvent.change(input, { target: { value: '/investigate f-1' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByText('Run incident response for f-1')).toBeInTheDocument()
    const run = screen.getByRole('button', { name: 'Run' })
    await waitFor(() => expect(run).toHaveFocus())
    expect(execute).not.toHaveBeenCalled()
    fireEvent.click(run)
    await waitFor(() => expect(execute).toHaveBeenCalledWith('incident-response', { finding_id: 'f-1' }))

    execute.mockClear()
    fireEvent.change(input, { target: { value: '/investigate not a finding' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: 'Run' }))
    await waitFor(() => expect(execute).toHaveBeenCalledWith('incident-response', { context: 'not a finding' }))

    execute.mockClear()
    fireEvent.change(input, { target: { value: '/hunt rare beacon' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: 'Run' }))
    await waitFor(() => expect(execute).toHaveBeenCalledWith('threat-hunt', { hypothesis: 'rare beacon' }))

    fireEvent.change(input, { target: { value: '/ask where did it go' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: 'Run' }))
    await waitFor(() => expect(onOpenChat).toHaveBeenCalledWith('where did it go'))
  })

  it('disables /ticket when project_key is missing and names the gap', async () => {
    getIntegrations.mockResolvedValue({
      data: {
        enabled_integrations: ['jira'],
        integrations: { jira: { url: 'https://jira.example', username: 'ada', project_key: '' } },
        secrets_set: { jira: { api_token: true } },
      },
    })
    renderBar()
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '/ticket case-9' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByText('Jira is missing project_key')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled()
  })

  it('opens the case drawer from /replay, expands to the cases route, and closes in place', async () => {
    render(
      <MemoryRouter initialEntries={['/dashboard']}>
        <Routes>
          <Route path="/:screen" element={<Shell />} />
        </Routes>
      </MemoryRouter>,
    )
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '/replay case-9' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: 'Run' }))
    expect(await screen.findByRole('heading', { name: 'Exact case' })).toBeInTheDocument()
    expect(screen.getByText('Dashboard page')).toBeInTheDocument()
    expect(screen.getByTestId('where')).toHaveTextContent('/dashboard')

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog', { name: 'Case' })).not.toBeInTheDocument()
    expect(screen.getByTestId('where')).toHaveTextContent('/dashboard')

    fireEvent.change(input, { target: { value: '/replay case-9' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('button', { name: 'Run' }))
    await screen.findByRole('dialog', { name: 'Case' })
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }))
    expect(screen.getByTestId('where')).toHaveTextContent('/cases?case=case-9')
    expect(screen.queryByRole('dialog', { name: 'Case' })).not.toBeInTheDocument()
  })
})
