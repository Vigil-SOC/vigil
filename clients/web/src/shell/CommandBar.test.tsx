import { useState, type ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import CommandBar from './CommandBar'
import CaseDrawer from './CaseDrawer'
import { ToastProvider } from './toast'
import type { BoardLink } from './commandBarModel'

const { execute, createCase, deleteCase, readDoc, checkCoverage, attachDocument, getCase, getFinding, getIntegrations, apiGet } = vi.hoisted(() => ({
  execute: vi.fn((..._args: unknown[]) => Promise.resolve({ data: {} })),
  createCase: vi.fn((..._args: unknown[]) => Promise.resolve({ data: { case_id: 'case-new' } })),
  deleteCase: vi.fn((..._args: unknown[]) => Promise.resolve({ data: {} })),
  readDoc: vi.fn((..._args: unknown[]) => Promise.resolve({ data: { text: '', pages: 1, condensed: false } })),
  checkCoverage: vi.fn((..._args: unknown[]) => Promise.resolve({ data: {} })),
  attachDocument: vi.fn((..._args: unknown[]) => Promise.resolve({ data: {} })),
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
    create: (data: unknown) => createCase(data),
    delete: (id: string) => deleteCase(id),
    attachDocument: (id: string, file: File, opts: unknown) => attachDocument(id, file, opts),
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
    listAll: vi.fn(() => Promise.resolve({ data: { workflows: [] } })),
    execute: (id: string, params: unknown) => execute(id, params),
    readHuntDocument: (file: File) => readDoc(file),
    checkCoverage: (body: unknown) => checkCoverage(body),
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
  createCase.mockClear()
  deleteCase.mockClear()
  readDoc.mockReset()
  checkCoverage.mockReset()
  attachDocument.mockReset()
  attachDocument.mockResolvedValue({ data: {} })
  createCase.mockResolvedValue({ data: { case_id: 'case-new' } })
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

  describe('/hunt', () => {
    const HYPOTHESIS = 'a service account key was used from a new network and then read customer exports'

    function renderHunt() {
      const onOpenCase = vi.fn()
      render(
        <ToastProvider>
          <CommandBar boards={BOARDS} onOpenChat={vi.fn()} onOpenCase={onOpenCase} onGo={vi.fn()} />
        </ToastProvider>,
      )
      return { onOpenCase, input: screen.getByRole('combobox') as HTMLInputElement }
    }

    async function runHunt(input: HTMLInputElement, text: string) {
      fireEvent.change(input, { target: { value: `/hunt ${text}` } })
      fireEvent.keyDown(input, { key: 'Enter' })
      fireEvent.click(await screen.findByRole('button', { name: 'Run' }))
    }

    it('opens a case with a short title, runs the hunt on it, and clears the bar', async () => {
      const { onOpenCase, input } = renderHunt()
      await runHunt(input, HYPOTHESIS)
      await waitFor(() => expect(onOpenCase).toHaveBeenCalledWith('case-new'))
      expect(createCase).toHaveBeenCalledWith({
        title: 'a service account key was used from a new network and then',
        description: HYPOTHESIS,
        finding_ids: [],
        priority: 'medium',
        status: 'open',
      })
      expect(execute).toHaveBeenCalledWith('threat-hunt', { hypothesis: HYPOTHESIS, case_id: 'case-new' })
      expect(createCase.mock.invocationCallOrder[0]).toBeLessThan(execute.mock.invocationCallOrder[0])
      expect(input.value).toBe('')
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
      expect(await screen.findByText(/Hunt started on case "a service account key/)).toBeInTheDocument()
      expect(deleteCase).not.toHaveBeenCalled()
    })

    it('removes the case when the server refuses the hunt, and leaves the preview open', async () => {
      execute.mockRejectedValue({ response: { data: { detail: 'Workflow threat-hunt is disabled' } } })
      const { onOpenCase, input } = renderHunt()
      await runHunt(input, 'credential access')
      expect(await screen.findByText('Workflow threat-hunt is disabled')).toBeInTheDocument()
      expect(deleteCase).toHaveBeenCalledWith('case-new')
      expect(onOpenCase).not.toHaveBeenCalled()
      expect(input.value).toBe('/hunt credential access')
      expect(screen.getByRole('button', { name: 'Run' })).toBeEnabled()
    })

    it('keeps the case when the request got no answer, since the run may be queued', async () => {
      execute.mockRejectedValue(new Error('timeout of 120000ms exceeded'))
      const { input } = renderHunt()
      await runHunt(input, 'credential access')
      expect(await screen.findByText('timeout of 120000ms exceeded')).toBeInTheDocument()
      expect(deleteCase).not.toHaveBeenCalled()
    })

    it('starts no run when the case cannot be created', async () => {
      createCase.mockRejectedValue({ response: { data: { detail: 'Failed to create case' } } })
      const { input } = renderHunt()
      await runHunt(input, HYPOTHESIS)
      expect(await screen.findByText('Failed to create case')).toBeInTheDocument()
      expect(execute).not.toHaveBeenCalled()
    })

    describe('attached intelligence', () => {
      const REPORT = 'Beacon to 203.0.113.9 using T1071'
      const PROPOSED = 'Activity from the reported indicators ip:203.0.113.9 is present in the environment'

      function pdf(name = 'advisory.pdf') {
        return new File(['%PDF'], name, { type: 'application/pdf' })
      }

      async function openHunt(input: HTMLInputElement, text = '') {
        fireEvent.change(input, { target: { value: `/hunt ${text}`.trimEnd() } })
        fireEvent.keyDown(input, { key: 'Enter' })
        await screen.findByRole('button', { name: 'Attach intelligence' })
      }

      function pick(file: File) {
        fireEvent.change(screen.getByLabelText('Attach intelligence file'), { target: { files: [file] } })
      }

      beforeEach(() => {
        readDoc.mockResolvedValue({ data: { text: REPORT, pages: 3, condensed: false } })
        checkCoverage.mockResolvedValue({ data: { status: 'uncovered', proposal: { hypothesis: PROPOSED, hypothesis_subjects: { [PROPOSED]: ['ip:203.0.113.9'] }, approve_hypotheses: true } } })
      })

      it('offers the control on /hunt only', async () => {
        const { input } = renderHunt()
        fireEvent.change(input, { target: { value: '/ask what' } })
        fireEvent.keyDown(input, { key: 'Enter' })
        await screen.findByRole('button', { name: 'Run' })
        expect(screen.queryByRole('button', { name: 'Attach intelligence' })).not.toBeInTheDocument()
      })

      it('reads a picked file, shows its name and pages, and sends the text with the hunt', async () => {
        const { onOpenCase, input } = renderHunt()
        await openHunt(input, 'credential access')
        const file = pdf()
        pick(file)
        expect(await screen.findByText('advisory.pdf')).toBeInTheDocument()
        expect(screen.getByText('3 pages')).toBeInTheDocument()
        expect(checkCoverage).not.toHaveBeenCalled()
        fireEvent.click(screen.getByRole('button', { name: 'Run' }))
        await waitFor(() => expect(onOpenCase).toHaveBeenCalledWith('case-new'))
        expect(execute).toHaveBeenCalledWith('threat-hunt', { hypothesis: 'credential access', case_id: 'case-new', document: REPORT })
        expect(attachDocument).toHaveBeenCalledWith('case-new', file, { name: undefined, pages: 3 })
        expect(execute.mock.invocationCallOrder[0]).toBeLessThan(attachDocument.mock.invocationCallOrder[0])
      })

      it('takes a file dropped on the preview', async () => {
        const { input } = renderHunt()
        await openHunt(input)
        const file = pdf('dropped.pdf')
        fireEvent.drop(screen.getByRole('button', { name: 'Run' }).closest('.vg-command-preview') as HTMLElement, { dataTransfer: { files: [file] } })
        expect(await screen.findByText('dropped.pdf')).toBeInTheDocument()
        expect(readDoc).toHaveBeenCalledWith(file)
      })

      it('attaches pasted text as "Pasted text"', async () => {
        const { input } = renderHunt()
        await openHunt(input, 'credential access')
        fireEvent.click(screen.getByRole('button', { name: 'Paste text' }))
        fireEvent.change(screen.getByLabelText('Intelligence text'), { target: { value: REPORT } })
        fireEvent.click(screen.getByRole('button', { name: 'Attach text' }))
        expect(await screen.findByText('Pasted text')).toBeInTheDocument()
        fireEvent.click(screen.getByRole('button', { name: 'Run' }))
        await waitFor(() => expect(attachDocument).toHaveBeenCalled())
        expect(attachDocument.mock.calls[0][2]).toEqual({ name: 'Pasted text', pages: 3 })
      })

      it('proposes a hypothesis from the document when none is typed, and runs with it', async () => {
        const { input } = renderHunt()
        await openHunt(input)
        pick(pdf())
        expect(await screen.findByText(`Start a threat hunt: ${PROPOSED}`)).toBeInTheDocument()
        expect(checkCoverage).toHaveBeenCalledWith({ report: REPORT })
        expect(screen.getByText(/Proposed from the document/)).toBeInTheDocument()
        fireEvent.click(screen.getByRole('button', { name: 'Run' }))
        await waitFor(() => expect(execute).toHaveBeenCalled())
        expect(execute).toHaveBeenCalledWith('threat-hunt', { hypothesis: PROPOSED, case_id: 'case-new', document: REPORT, hypothesis_subjects: { [PROPOSED]: ['ip:203.0.113.9'] }, approve_hypotheses: true })
      })

      it('says why there is no proposal and keeps Run disabled', async () => {
        checkCoverage.mockRejectedValueOnce({ response: { status: 400, data: { detail: 'nothing to check' } } })
        const { input } = renderHunt()
        await openHunt(input)
        pick(pdf())
        expect(await screen.findByText(/Nothing in this document to propose a hypothesis from/)).toBeInTheDocument()
        expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled()
      })

      it('names a hunt already running and offers no proposal', async () => {
        checkCoverage.mockResolvedValueOnce({ data: { status: 'running', in_flight: [] } })
        const { input } = renderHunt()
        await openHunt(input)
        pick(pdf())
        expect(await screen.findByText(/A hunt is already running on what this document covers/)).toBeInTheDocument()
        expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled()
      })

      it('shows a refusal in the preview and clears it with Remove', async () => {
        readDoc.mockRejectedValueOnce({ response: { status: 422, data: { detail: 'brief.docx is an Office file, and reading it needs LibreOffice, which is not installed on this server.' } } })
        const { input } = renderHunt()
        await openHunt(input, 'credential access')
        pick(new File(['x'], 'brief.docx'))
        expect(await screen.findByText(/needs LibreOffice/)).toBeInTheDocument()
        expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled()
        fireEvent.click(screen.getByRole('button', { name: 'Remove brief.docx' }))
        expect(screen.getByRole('button', { name: 'Run' })).toBeEnabled()
      })

      it('refuses a wrong type without sending it', async () => {
        const { input } = renderHunt()
        await openHunt(input, 'credential access')
        pick(new File(['MZ'], 'tool.exe'))
        expect(await screen.findByText(/\.exe files cannot be attached/)).toBeInTheDocument()
        expect(readDoc).not.toHaveBeenCalled()
      })

      it('keeps no original on the case when the server refuses the hunt', async () => {
        execute.mockRejectedValue({ response: { data: { detail: 'Workflow threat-hunt is disabled' } } })
        const { input } = renderHunt()
        await openHunt(input, 'credential access')
        pick(pdf())
        await screen.findByText('advisory.pdf')
        fireEvent.click(screen.getByRole('button', { name: 'Run' }))
        expect(await screen.findByText('Workflow threat-hunt is disabled')).toBeInTheDocument()
        expect(deleteCase).toHaveBeenCalledWith('case-new')
        expect(attachDocument).not.toHaveBeenCalled()
        expect(screen.getByText('advisory.pdf')).toBeInTheDocument()
      })

      it('says so when the hunt started but the original could not be kept', async () => {
        attachDocument.mockRejectedValue({ response: { data: { detail: 'The document could not be kept on the case' } } })
        const { onOpenCase, input } = renderHunt()
        await openHunt(input, 'credential access')
        pick(pdf())
        await screen.findByText('advisory.pdf')
        fireEvent.click(screen.getByRole('button', { name: 'Run' }))
        expect(await screen.findByText(/could not be kept on the case: The document could not be kept on the case/)).toBeInTheDocument()
        expect(onOpenCase).toHaveBeenCalledWith('case-new')
      })
    })
  })
})
