import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { format } from 'date-fns'
import Chat from './Chat'
import api, { conversationsApi, reasoningApi, streamFetch } from '../services/api'

const historyState = vi.hoisted(() => ({
  items: [] as Array<{
    id: string
    title: string | null
    message_count: number
    last_message_at: string | null
    updated_at: string | null
    archived: boolean
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
  analyticsApi: { estimateCost: vi.fn(() => new Promise(() => undefined)) },
  claudeApi: { getModels: vi.fn(() => new Promise(() => undefined)) },
  conversationsApi: {
    get: vi.fn(),
    list: vi.fn(),
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

describe('Ask Vigil dock', () => {
  it('drops the model, prompt, cost, and agent controls', () => {
    renderChat()
    expect(screen.getByText('Private to you')).toBeInTheDocument()
    expect(screen.getByText('Using Overview')).toBeInTheDocument()
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

    expect(screen.getByTestId('attached-case')).toHaveTextContent('CASE-9')
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

  it('groups history into calendar days and reopens that conversation', async () => {
    const older = '2026-03-01T15:00:00Z'
    const newer = '2026-03-02T15:00:00Z'
    historyState.items = [
      { id: 'newer', title: 'Monday thread', message_count: 2, last_message_at: newer, updated_at: null, archived: false },
      { id: 'older', title: 'Sunday thread', message_count: 1, last_message_at: older, updated_at: null, archived: false },
    ]
    vi.mocked(conversationsApi.get).mockResolvedValue({
      data: { id: 'newer', messages: [], case_id: null },
    } as never)

    renderChat()
    fireEvent.click(screen.getByTitle('History'))

    expect(screen.getByText(format(new Date(newer), 'MMM d, yyyy'))).toBeInTheDocument()
    expect(screen.getByText(format(new Date(older), 'MMM d, yyyy'))).toBeInTheDocument()
    fireEvent.click(screen.getByText('Monday thread'))
    await waitFor(() => expect(conversationsApi.get).toHaveBeenCalledWith('newer'))
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

    render(<Chat open onClose={vi.fn()} pageKey="overview" pageTitle="Overview" />)

    fireEvent.click(screen.getByTitle('Reasoning trace'))

    expect(await screen.findByText('not priced')).toBeInTheDocument()
    const header = document.querySelector('.trace-sum')
    expect(header?.textContent).toContain('1 unpriced')
    expect(header?.textContent).not.toContain('$0.0000')
  })
})
