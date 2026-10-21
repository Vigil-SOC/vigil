import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { format } from 'date-fns'
import Chat from './Chat'
import type { Citation } from './citations'
import api, { conversationsApi, reasoningApi, streamFetch } from '../services/api'

const historyState = vi.hoisted(() => ({
  items: [] as Array<{
    id: string
    title: string | null
    message_count: number
    last_message_at: string | null
    updated_at: string | null
    archived: boolean
    case_id: string | null
    page_context: string | null
  }>,
}))

vi.mock('./useConversations', () => ({
  useConversations: () => ({ items: historyState.items, phase: 'ready', error: null, reload: vi.fn() }),
}))

vi.mock('../services/notifications', () => ({
  notificationService: { notifyInvestigationComplete: vi.fn() },
}))

vi.mock('../services/api', () => ({
  agentsApi: { listAgents: vi.fn(() => new Promise(() => undefined)) },
  aiConfigApi: { getConfig: vi.fn(() => new Promise(() => undefined)) },
  conversationsApi: {
    list: vi.fn(() => new Promise(() => undefined)),
    get: vi.fn(),
    delete: vi.fn(),
    update: vi.fn(),
    importHistory: vi.fn(),
  },
  reasoningApi: {
    listInteractions: vi.fn(),
    getSessionSummary: vi.fn(),
    getInteraction: vi.fn(),
  },
  streamFetch: vi.fn(),
  default: { get: vi.fn() },
}))

beforeEach(() => {
  historyState.items = []
  vi.mocked(api.get).mockReset()
  vi.mocked(streamFetch).mockReset()
  vi.mocked(conversationsApi.update).mockReset()
  vi.mocked(conversationsApi.update).mockResolvedValue({ data: {} } as never)
  vi.mocked(conversationsApi.get).mockReset()
  vi.mocked(reasoningApi.getSessionSummary).mockReset()
  vi.mocked(reasoningApi.getSessionSummary).mockResolvedValue(null)
})

function renderChat(props: { pageKey?: string; pageTitle?: string } = {}) {
  return render(
    <Chat open onClose={vi.fn()} pageKey={props.pageKey ?? 'overview'} pageTitle={props.pageTitle ?? 'Overview'} />,
  )
}

function emptyStream(): Response {
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: () => Promise.resolve({ done: true, value: undefined }),
      }),
    },
  } as unknown as Response
}

function replyStream(text: string): Response {
  const chunk = new TextEncoder().encode(`data: ${JSON.stringify({ type: 'text', content: text })}\n`)
  let sent = false
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: () =>
          Promise.resolve(sent ? { done: true, value: undefined } : ((sent = true), { done: false, value: chunk })),
      }),
    },
  } as unknown as Response
}

describe('Ask Vigil dock', () => {
  it('has an Ask Vigil header with history, new conversation and close only', () => {
    renderChat()
    const dialog = screen.getByRole('dialog', { name: 'Ask Vigil' })
    const head = dialog.querySelector('.chat-head') as HTMLElement
    expect(within(head).getByRole('heading', { name: 'Ask Vigil' })).toBeInTheDocument()
    expect(within(head).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
      'Conversation history',
      'New conversation',
      'Close Ask Vigil',
    ])
    expect(screen.queryByTitle('SOC Agents')).toBeNull()
    expect(within(head).getByText('New conversation', { selector: '.ch-convo' })).toBeInTheDocument()
  })

  it('shows the page chip, the saved-privately line and an @ button but no slash', () => {
    renderChat()
    expect(screen.getByText('Using this page')).toBeInTheDocument()
    expect(document.querySelector('.cx-pill')).toHaveTextContent('Overview')
    expect(screen.getByText('Saved privately to your history · @ attaches · Enter sends')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '/' })).toBeNull()
    expect(screen.queryByText('Private to you')).toBeNull()
  })

  it('puts @ in the draft from the @ button and prompts for a case', async () => {
    renderChat()
    const box = screen.getByPlaceholderText(/Ask Vigil/) as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'look at' } })
    fireEvent.click(screen.getByRole('button', { name: 'Mention a case' }))
    expect(box.value).toBe('look at @')
    expect(box).toHaveFocus()
    expect(await screen.findByText('Type a case id or title')).toBeInTheDocument()
    expect(screen.queryByText('No matching cases')).toBeNull()
  })

  it('drops the model, prompt, cost, and agent controls', () => {
    renderChat()
    expect(screen.queryByTitle('Chat settings')).toBeNull()
    expect(screen.queryByPlaceholderText(/Override default system prompt/)).toBeNull()
    expect(screen.queryByText(/k tokens/)).toBeNull()
    expect(screen.queryByRole('button', { name: /Default agent/ })).toBeNull()
    expect(document.querySelector('.model-sel')).toBeNull()
    expect(document.querySelector('.cm-cost')).toBeNull()
    expect(document.querySelector('.case-composer')).toBeNull()
    expect(document.querySelector('.composer-note')).toBeNull()
  })

  it('stores a case id from full-text search and ignores a typed id that was not returned', async () => {
    vi.mocked(api.get).mockImplementation((path: string, config?: { params?: unknown }) => {
      const query = (config?.params as { query?: string } | undefined)?.query
      if (path === '/cases/search/full-text' && query === 'loader') {
        return Promise.resolve({
          data: {
            cases: [{ case_id: 'CASE-9', title: 'Obfuscated loader' }],
            comments: [],
            evidence: [],
          },
        })
      }
      return Promise.resolve({ data: { cases: [], comments: [], evidence: [] } })
    })
    vi.mocked(streamFetch).mockResolvedValue(emptyStream())

    renderChat()
    const box = screen.getByPlaceholderText(/Ask Vigil/)
    expect(box).toHaveAttribute('placeholder', 'Ask Vigil · @ to attach a case')
    fireEvent.change(box, { target: { value: '@loader' } })
    fireEvent.click(await screen.findByRole('option', { name: /CASE-9/ }))

    expect(screen.getByTestId('attached-case')).toHaveTextContent('@CASE-9')
    expect(conversationsApi.update).not.toHaveBeenCalled()

    fireEvent.change(box, { target: { value: '@NOPE-1' } })
    await screen.findByText('No matching cases')
    fireEvent.keyDown(box, { key: 'Enter' })

    await waitFor(() => expect(streamFetch).toHaveBeenCalled())
    const body = JSON.parse((vi.mocked(streamFetch).mock.calls[0][1] as { body: string }).body)
    expect(body.case_id).toBe('CASE-9')
    expect(body.page_context).toBe('overview')
    expect(body.model).toBeUndefined()
    expect(body.system_prompt).toBeUndefined()
    expect(body.max_tokens).toBeUndefined()
    expect(body.agent_id).toBeUndefined()
    expect(screen.getByTestId('attached-case')).toHaveTextContent('CASE-9')
    expect(screen.getByTestId('attached-case')).not.toHaveTextContent('NOPE-1')
  })

  it('patches a case chosen while the reply is still streaming', async () => {
    let release: (row: { done: boolean; value?: undefined }) => void = () => undefined
    const gate = new Promise<{ done: boolean; value?: undefined }>((resolve) => {
      release = resolve
    })
    vi.mocked(api.get).mockResolvedValue({
      data: { cases: [{ case_id: 'CASE-9', title: 'Obfuscated loader' }], comments: [], evidence: [] },
    })
    vi.mocked(streamFetch).mockResolvedValue({
      ok: true,
      status: 200,
      body: { getReader: () => ({ read: () => gate }) },
    } as unknown as Response)

    renderChat()
    const box = screen.getByPlaceholderText(/Ask Vigil/)
    fireEvent.change(box, { target: { value: 'hello' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await screen.findByTitle('Stop')

    fireEvent.change(box, { target: { value: '@loader' } })
    fireEvent.click(await screen.findByRole('option', { name: /CASE-9/ }))
    expect(conversationsApi.update).not.toHaveBeenCalled()

    release({ done: true })
    await waitFor(() =>
      expect(conversationsApi.update).toHaveBeenCalledWith(expect.any(String), { case_id: 'CASE-9' }),
    )
  })

  it('opens history inside the dock, grouped by day with a context per row, and reopens a conversation', async () => {
    const older = '2026-03-01T15:00:00Z'
    const newer = '2026-03-02T15:00:00Z'
    const row = (id: string, title: string, at: string, case_id: string | null, page_context: string | null) => ({
      id, title, message_count: 2, last_message_at: at, updated_at: null, archived: false, case_id, page_context,
    })
    historyState.items = [
      row('newer', 'Monday thread', newer, 'CASE-9', 'cases'),
      row('page', 'Triage thread', older, null, 'triage'),
      row('none', 'Sunday thread', older, null, null),
    ]
    vi.mocked(conversationsApi.get).mockResolvedValue({
      data: { id: 'newer', messages: [], case_id: null },
    } as never)

    renderChat()
    const toggle = screen.getByRole('button', { name: 'Conversation history' })
    fireEvent.click(toggle)

    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByRole('dialog', { name: 'Conversation history' })).toBeNull()
    expect(screen.getByText('Private to you in this workspace. Saved on the server.')).toBeInTheDocument()
    expect(screen.getByText(format(new Date(newer), 'MMM d, yyyy'))).toBeInTheDocument()
    expect(screen.getByText(format(new Date(older), 'MMM d, yyyy'))).toBeInTheDocument()
    expect(screen.getByText('Case CASE-9')).toBeInTheDocument()
    expect(screen.getByText('Triage queue')).toBeInTheDocument()
    expect(screen.getByText('General')).toBeInTheDocument()
    expect(screen.queryByPlaceholderText(/Ask Vigil/)).toBeNull()

    fireEvent.click(screen.getByText('Monday thread'))
    await waitFor(() => expect(conversationsApi.get).toHaveBeenCalledWith('newer'))
    await screen.findByPlaceholderText(/Ask Vigil/)
    expect(screen.getByText('Monday thread', { selector: '.ch-convo' })).toBeInTheDocument()
  })

  it('says when a search matches no conversation, and Esc returns to the conversation', () => {
    const onClose = vi.fn()
    render(<Chat open onClose={onClose} pageKey="overview" pageTitle="Overview" />)
    fireEvent.click(screen.getByRole('button', { name: 'Conversation history' }))
    fireEvent.change(screen.getByLabelText('Search conversations'), { target: { value: 'zzz' } })
    expect(screen.getByText('No conversations match.')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByPlaceholderText(/Ask Vigil/)).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('clears the transcript on New conversation', async () => {
    vi.mocked(streamFetch).mockResolvedValue(replyStream('All quiet.'))
    renderChat()
    fireEvent.change(screen.getByPlaceholderText(/Ask Vigil/), { target: { value: 'status?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    // the Reasoning trace button marks the settled reply, not the streaming one
    await screen.findByTitle('Reasoning trace', {}, { timeout: 5000 })
    expect(screen.getByText('All quiet.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }))
    expect(screen.queryByText('All quiet.')).toBeNull()
    expect(screen.getByText('Ask about what you are looking at')).toBeInTheDocument()
  })
})

describe('outside a secure context', () => {
  it('renders and sends a v4 session id when crypto.randomUUID is missing', async () => {
    // plain HTTP at a LAN address: browsers leave randomUUID undefined
    vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) })
    try {
      vi.mocked(streamFetch).mockResolvedValue(emptyStream())
      renderChat()
      fireEvent.change(screen.getByPlaceholderText(/Ask Vigil/), { target: { value: 'status?' } })
      fireEvent.click(screen.getByRole('button', { name: 'Send' }))
      await waitFor(() => expect(streamFetch).toHaveBeenCalled())
      const body = JSON.parse(String(vi.mocked(streamFetch).mock.calls[0][1]?.body))
      expect(body.session_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('case composer', () => {
  it('keeps its private note and has no @ button', () => {
    render(<Chat pinned open onClose={vi.fn()} pageKey="cases" lockedCaseId="CASE-9" />)
    expect(screen.getByText('Private to you · Ask only')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Mention a case' })).toBeNull()
    expect(screen.queryByText(/Saved privately/)).toBeNull()
  })
})

describe('pinned case composer', () => {
  function renderPinned() {
    vi.mocked(conversationsApi.list).mockResolvedValue({ data: { conversations: [] } } as never)
    return render(<Chat pinned open onClose={vi.fn()} pageKey="cases" lockedCaseId="CASE-1" />)
  }

  it('starts as one row with the note, Ask only, and no fold', () => {
    renderPinned()
    expect(screen.getByText('Private to you · Ask only')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Tell' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Do' })).toBeDisabled()
    expect(document.querySelector('.composer-fold')).toBeNull()
    expect(document.querySelector('.chat-body')).toBeNull()
    expect(screen.queryByTestId('attached-case')).toBeNull()
  })

  it('shows Stop while loading, then the error and a fold that hides the thread', async () => {
    let fail: (e: Error) => void = () => undefined
    vi.mocked(streamFetch).mockReturnValue(new Promise((_, reject) => { fail = reject }))
    renderPinned()
    fireEvent.change(screen.getByPlaceholderText('Ask about this case'), { target: { value: 'why?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await screen.findByTitle('Stop')

    fail(new Error('boom'))
    expect(await screen.findByText(/Could not reach Vigil: boom/)).toBeInTheDocument()
    const fold = screen.getByRole('button', { name: /2 messages · hide/ })
    fireEvent.click(fold)
    expect(screen.queryByText(/Could not reach Vigil/)).toBeNull()
    expect(fold).toHaveTextContent('2 messages · show')
  })

  it('shows an error frame from the server as sent, without blaming the backend', async () => {
    const frame = new TextEncoder().encode('data: {"error":"This case has more than Ask can read at once."}\n\n')
    let sent = false
    vi.mocked(streamFetch).mockResolvedValue({
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: () => {
            const row = sent ? { done: true, value: undefined } : { done: false, value: frame }
            sent = true
            return Promise.resolve(row)
          },
        }),
      },
    } as unknown as Response)
    renderPinned()
    fireEvent.change(screen.getByPlaceholderText('Ask about this case'), { target: { value: 'why?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect(await screen.findByText('This case has more than Ask can read at once.')).toBeInTheDocument()
    expect(screen.queryByText(/Could not reach Vigil|Is the backend running/)).toBeNull()
  })
})

describe('failed case-composer turns', () => {
  const refused = (status: number, body: unknown) =>
    ({ ok: false, status, json: () => Promise.resolve(body) }) as unknown as Response
  function renderPinned() {
    vi.mocked(conversationsApi.list).mockResolvedValue({ data: { conversations: [] } } as never)
    return render(<Chat pinned open onClose={vi.fn()} pageKey="cases" lockedCaseId="CASE-1" />)
  }
  const ask = (text: string) => {
    fireEvent.change(screen.getByPlaceholderText('Ask about this case'), { target: { value: text } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
  }

  it('shows a refusal in its own words, not as an unreachable backend, and Retry resends the question', async () => {
    vi.mocked(streamFetch).mockResolvedValueOnce(refused(402, { detail: 'Daily budget reached.' }))
    renderPinned()
    ask('why?')
    expect(await screen.findByText('Daily budget reached.')).toBeInTheDocument()
    expect(screen.queryByText(/Could not reach Vigil/)).toBeNull()

    vi.mocked(streamFetch).mockResolvedValueOnce(emptyStream())
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(streamFetch).toHaveBeenCalledTimes(2))
    const sent = JSON.parse(String(vi.mocked(streamFetch).mock.calls[1][1]?.body))
    expect(sent.messages).toEqual([{ role: 'user', content: 'why?' }])
  })

  it('blames the backend only for a 502 or 503', async () => {
    vi.mocked(streamFetch).mockResolvedValueOnce(refused(503, {}))
    renderPinned()
    ask('why?')
    expect(await screen.findByText(/Could not reach Vigil: HTTP 503\. Is the backend running\?/)).toBeInTheDocument()
  })

  it('does not send an unanswered question with the next one', async () => {
    vi.mocked(streamFetch).mockResolvedValueOnce(refused(402, { detail: 'No room.' }))
    renderPinned()
    ask('first')
    await screen.findByText('No room.')
    vi.mocked(streamFetch).mockResolvedValueOnce(emptyStream())
    ask('second')
    await waitFor(() => expect(streamFetch).toHaveBeenCalledTimes(2))
    const sent = JSON.parse(String(vi.mocked(streamFetch).mock.calls[1][1]?.body))
    expect(sent.messages).toEqual([{ role: 'user', content: 'second' }])
  })

  it('re-renders stored failed turns as failed, one bubble per question', async () => {
    vi.mocked(conversationsApi.list).mockResolvedValue({ data: { conversations: [{ id: 's1', case_id: 'CASE-1' }] } } as never)
    vi.mocked(conversationsApi.get).mockResolvedValue({
      data: {
        id: 's1',
        case_id: 'CASE-1',
        messages: [
          { role: 'user', content: 'first', complete: true },
          { role: 'assistant', content: 'No room.', complete: false },
          { role: 'user', content: 'second', complete: true },
          { role: 'assistant', content: '', complete: false },
        ],
      },
    } as never)
    render(<Chat pinned open onClose={vi.fn()} pageKey="cases" lockedCaseId="CASE-1" />)
    await screen.findByRole('button', { name: /4 messages/ })
    expect(screen.getByText('first')).toBeInTheDocument()
    expect(screen.getByText('second')).toBeInTheDocument()
    expect(screen.getByText('No room.')).toBeInTheDocument()
    expect(screen.getByText('This turn did not finish.')).toBeInTheDocument()
    expect(screen.queryByText('(no response)')).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Retry' })).toHaveLength(1)
  })

  it('folds to the last exchange when the tab changes, and shows the rest on request', async () => {
    vi.mocked(conversationsApi.list).mockResolvedValue({ data: { conversations: [{ id: 's1', case_id: 'CASE-1' }] } } as never)
    vi.mocked(conversationsApi.get).mockResolvedValue({
      data: {
        id: 's1',
        case_id: 'CASE-1',
        messages: [
          { role: 'user', content: 'first', complete: true },
          { role: 'assistant', content: 'one', complete: true },
          { role: 'user', content: 'second', complete: true },
          { role: 'assistant', content: 'two', complete: true },
        ],
      },
    } as never)
    const view = render(<Chat pinned open onClose={vi.fn()} pageKey="cases" lockedCaseId="CASE-1" collapseKey="Summary" />)
    await screen.findByText('first')
    view.rerender(<Chat pinned open onClose={vi.fn()} pageKey="cases" lockedCaseId="CASE-1" collapseKey="Evidence" />)
    expect(screen.queryByText('first')).toBeNull()
    expect(screen.getByText('second')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show 2 earlier messages' }))
    expect(screen.getByText('first')).toBeInTheDocument()
  })
})

describe('reasoning trace cost', () => {
  it('shows an all-unpriced session as not priced', async () => {
    vi.mocked(reasoningApi.getSessionSummary).mockResolvedValue({
      total_interactions: 1,
      total_cost_usd: null,
      unpriced_calls: 1,
      total_input_tokens: 10,
      total_output_tokens: 4,
    })
    vi.mocked(reasoningApi.listInteractions).mockResolvedValue({ interactions: [] })

    vi.mocked(streamFetch).mockResolvedValue(replyStream('All quiet.'))

    renderChat()
    fireEvent.change(screen.getByPlaceholderText(/Ask Vigil/), { target: { value: 'status?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    fireEvent.click(await screen.findByTitle('Reasoning trace', {}, { timeout: 5000 }))

    expect(await screen.findByText('not priced')).toBeInTheDocument()
    const header = document.querySelector('.trace-sum')
    expect(header?.textContent).toContain('1 unpriced')
    expect(header?.textContent).not.toContain('$0.0000')
  })
})

describe('citation chips on the case composer (#2285)', () => {
  const citations: Citation[] = [
    { id: 'ev-0b17352e22bf', kind: 'evidence', label: 'step 3 · CrowdStrike', text: 'rundll32 loaded msupdate64.dll from Temp' },
    { id: 'ev-ceab2569df3a', kind: 'evidence', label: 'step 5 · Zscaler', text: 'HTTPS beacon to cdn-invoicesync.net every 60 s' },
    { id: 'h-fb8e2227', kind: 'explanation', label: 'explanation · DUSKY LANTERN on FIN-WS-114', text: 'DUSKY LANTERN on FIN-WS-114' },
    { id: 'dl-005', kind: 'alert', label: 'alert · LSASS MiniDump', text: 'comsvcs.dll MiniDump of lsass.exe', href: '/overview?alert=dl-005' },
  ]

  function renderAnswer(answer: string, onCite = vi.fn()) {
    vi.mocked(conversationsApi.list).mockResolvedValue({ data: { conversations: [{ id: 's1', case_id: 'CASE-1' }] } } as never)
    vi.mocked(conversationsApi.get).mockResolvedValue({
      data: {
        id: 's1',
        case_id: 'CASE-1',
        messages: [
          { role: 'user', content: 'what happened?', complete: true },
          { role: 'assistant', content: answer, complete: true },
        ],
      },
    } as never)
    render(<Chat pinned open onClose={vi.fn()} pageKey="cases" lockedCaseId="CASE-1" citations={citations} onCite={onCite} />)
    return onCite
  }

  it('names the step and source a row came from, with its text on hover, never the raw id, once per answer', async () => {
    renderAnswer('The DLL loaded (ev-0b17352e22bf), then it beaconed (`ev-ceab2569df3a`).')
    const body = await waitFor(() => {
      const el = document.querySelector('.msg.vigil .body .cite-chip')
      expect(el).not.toBeNull()
      return el?.closest('.body') as HTMLElement
    })
    expect(body.textContent).not.toMatch(/ev-[0-9a-f]{12}/)
    // where the answer wrote each id, prose or code span, its chip stands in the line, and nowhere else (#2565)
    const chips = within(body).getAllByRole('button')
    expect(chips.map((chip) => chip.textContent)).toEqual(['step 3 · CrowdStrike', 'step 5 · Zscaler'])
    expect(chips[0]).toHaveAttribute('title', 'rundll32 loaded msupdate64.dll from Temp')
    expect(document.querySelectorAll('.msg.vigil .cite-chip')).toHaveLength(2)
  })

  it('drops the kind word written before an alert or explanation chip, and a name repeated in brackets (#2565)', async () => {
    renderAnswer('The alert dl-005 backs the explanation **h-fb8e2227**; alerts dl-005 again. Zscaler (Zscaler) saw `\\\\fs01\\Payments$`.')
    const body = await waitFor(() => {
      const el = document.querySelector('.msg.vigil .body .cite-chip')
      expect(el).not.toBeNull()
      return el?.closest('.body') as HTMLElement
    })
    expect(body.textContent).toBe(
      'The alert · LSASS MiniDump backs the explanation · DUSKY LANTERN on FIN-WS-114; alerts LSASS MiniDump again. Zscaler saw \\\\fs01\\Payments$.',
    )
  })

  it('chips the alerts and explanations the answer names where it names them, and leaves an id the case does not hold as text', async () => {
    const onCite = renderAnswer('dl-005 supports h-fb8e2227; ghost-9 is not on this case.')
    const row = await waitFor(() => {
      const el = document.querySelector('.msg.vigil .body .cite-chip')
      expect(el).not.toBeNull()
      return el?.closest('.body') as HTMLElement
    })
    const alert = within(row).getByRole('link', { name: 'alert · LSASS MiniDump' })
    expect(alert).toHaveAttribute('href', '/overview?alert=dl-005')
    expect(alert).toHaveAttribute('target', '_blank')
    fireEvent.click(within(row).getByRole('button', { name: 'explanation · DUSKY LANTERN on FIN-WS-114' }))
    expect(onCite).toHaveBeenCalledWith(expect.objectContaining({ id: 'h-fb8e2227', kind: 'explanation' }))
    expect(screen.getByText(/ghost-9 is not on this case/)).toBeInTheDocument()
  })

  // #2459: the same alert chipped three times in a paragraph, with its raw source id printed after it.
  it('chips an alert once per paragraph, names it again in words, and drops the raw source id beside it', async () => {
    renderAnswer(
      '**dl-005** (crowdstrike-dl-005, critical): the dump. dl-005 came before the SMB session, and `dl-005` is the strongest row.\n\n' +
        'dl-005 (crowdstrike-dl-005) again.',
    )
    const body = await waitFor(() => {
      const el = document.querySelector('.msg.vigil .body .cite-chip')
      expect(el).not.toBeNull()
      return el?.closest('.body') as HTMLElement
    })
    const [first, second] = Array.from(body.querySelectorAll('p'))
    expect(within(first).getAllByRole('link', { name: 'alert · LSASS MiniDump' })).toHaveLength(1)
    expect(first.querySelectorAll('.cite-again')).toHaveLength(2)
    expect(first.querySelector('.cite-again')).toHaveTextContent('LSASS MiniDump')
    expect(first.textContent).toContain('(critical): the dump.')
    // a new paragraph chips it again, and a trailer of nothing but the id goes whole
    expect(within(second).getAllByRole('link', { name: 'alert · LSASS MiniDump' })).toHaveLength(1)
    expect(second.textContent).toBe('alert · LSASS MiniDump again.')
    expect(body.textContent).not.toMatch(/crowdstrike-dl-005|dl-005/)
  })

  it('opens the row an evidence chip cites', async () => {
    const onCite = renderAnswer('See ev-ceab2569df3a.')
    fireEvent.click((await screen.findAllByRole('button', { name: 'step 5 · Zscaler' }))[0])
    expect(onCite).toHaveBeenCalledWith(expect.objectContaining({ id: 'ev-ceab2569df3a', kind: 'evidence' }))
  })
})

describe('case composer thread height (#2292)', () => {
  beforeEach(() => {
    localStorage.removeItem('soc.chat.threadHeight')
    // jsdom has no PointerEvent, so a fired pointer event would carry no button or clientY
    if (!('PointerEvent' in window)) {
      class PointerEventShim extends MouseEvent {
        pointerId: number
        constructor(type: string, init: PointerEventInit = {}) {
          super(type, init)
          this.pointerId = init.pointerId ?? 0
        }
      }
      vi.stubGlobal('PointerEvent', PointerEventShim)
    }
  })

  async function renderThread() {
    vi.mocked(conversationsApi.list).mockResolvedValue({ data: { conversations: [{ id: 's1', case_id: 'CASE-1' }] } } as never)
    vi.mocked(conversationsApi.get).mockResolvedValue({
      data: {
        id: 's1',
        case_id: 'CASE-1',
        messages: [
          { role: 'user', content: 'what happened?', complete: true },
          { role: 'assistant', content: 'A long answer.', complete: true },
        ],
      },
    } as never)
    render(<Chat pinned open onClose={vi.fn()} pageKey="cases" lockedCaseId="CASE-1" />)
    await screen.findByText('A long answer.')
    return document.querySelector('.case-composer .chat-body') as HTMLElement
  }

  it('drags to a height the person chose, keeps it, and restores the default on double-click', async () => {
    const body = await renderThread()
    // jsdom lays nothing out: the thread's box is its style height, its bottom edge fixed at 800
    body.getBoundingClientRect = () => {
      const height = parseFloat(body.style.height) || 150
      return { top: 800 - height, bottom: 800, height, left: 0, right: 0, width: 0, x: 0, y: 800 - height, toJSON: () => ({}) }
    }
    const grip = screen.getByRole('separator', { name: 'Resize the thread' })
    fireEvent.pointerDown(grip, { button: 0, clientY: 646, pointerId: 1 })
    fireEvent.pointerMove(grip, { clientY: 396, pointerId: 1 })
    expect(body.style.height).toBe('400px')
    fireEvent.pointerUp(grip, { clientY: 396, pointerId: 1 })
    await waitFor(() => expect(localStorage.getItem('soc.chat.threadHeight')).toBe('400'))

    fireEvent.doubleClick(grip)
    expect(body.style.height).toBe('')
    expect(localStorage.getItem('soc.chat.threadHeight')).toBeNull()
  })

  it('resizes from the keyboard', async () => {
    const body = await renderThread()
    const grip = screen.getByRole('separator', { name: 'Resize the thread' })
    fireEvent.keyDown(grip, { key: 'ArrowUp' })
    fireEvent.keyDown(grip, { key: 'ArrowUp' })
    expect(body.style.height).toBe('144px')
    await waitFor(() => expect(localStorage.getItem('soc.chat.threadHeight')).toBe('144'))
  })

  it('collapses, which takes the thread and its grip away so the tab has the height', async () => {
    await renderThread()
    fireEvent.click(screen.getByRole('button', { name: /2 messages · hide/ }))
    expect(document.querySelector('.case-composer .chat-body')).toBeNull()
    expect(screen.queryByRole('separator', { name: 'Resize the thread' })).toBeNull()
    expect(document.querySelector('.case-composer')).not.toHaveClass('thread-open')
  })

  it('opens a remembered height on the next case', async () => {
    localStorage.setItem('soc.chat.threadHeight', '420')
    const body = await renderThread()
    expect(body.style.height).toBe('420px')
  })
})
