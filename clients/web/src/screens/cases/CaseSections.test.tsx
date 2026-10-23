import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { CommentsView, EvidenceCard, IOCsCard, TasksView, useComments, useTasks } from './CaseSections'
import { casesApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  casesApi: {
    getTasks: vi.fn(),
    updateTask: vi.fn(),
    addTask: vi.fn(),
    getComments: vi.fn(),
    addComment: vi.fn(),
    getEvidence: vi.fn(),
    addEvidence: vi.fn(),
    getIOCs: vi.fn(),
    addIOC: vi.fn(),
  },
}))

/** The shape `CaseTaskSchema` dumps — `task_id`, not `id`. */
const TASK_ROW = {
  task_id: 42,
  title: 'Collect packet capture',
  status: 'pending',
  priority: 'high',
}

function Tasks({ caseId }: { caseId: string }) {
  return <TasksView caseId={caseId} resource={useTasks(caseId)} />
}

beforeEach(() => {
  vi.mocked(casesApi.getTasks).mockResolvedValue({
    data: { tasks: [TASK_ROW] },
  } as never)
  vi.mocked(casesApi.updateTask).mockResolvedValue({ data: TASK_ROW } as never)
})

describe('TasksView', () => {
  it('sorts an unknown-priority task after a rated one', async () => {
    vi.mocked(casesApi.getTasks).mockResolvedValue({
      data: {
        tasks: [
          { ...TASK_ROW, task_id: 1, title: 'Unrated follow-up', priority: 'unknown' },
          { ...TASK_ROW, task_id: 2, title: 'Collect packet capture', priority: 'high' },
        ],
      },
    } as never)

    render(<Tasks caseId="case-2026-0142" />)

    await waitFor(() =>
      expect(screen.getByText('Collect packet capture')).toBeInTheDocument(),
    )
    const high = screen.getByText('Collect packet capture')
    const unrated = screen.getByText('Unrated follow-up')
    expect(high.compareDocumentPosition(unrated) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('toggles a task by task_id via PUT, not a hand-written id', async () => {
    render(<Tasks caseId="case-2026-0142" />)

    await waitFor(() =>
      expect(screen.getByText('Collect packet capture')).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTitle('Toggle'))
    await waitFor(() =>
      expect(casesApi.updateTask).toHaveBeenCalledWith('case-2026-0142', 42, {
        status: 'completed',
        completed_at: expect.any(String),
      }),
    )
  })
})

function Comments({ caseId }: { caseId: string }) {
  return <CommentsView caseId={caseId} resource={useComments(caseId)} />
}

describe('comment and evidence writes', () => {
  it('posts a comment without an author', async () => {
    vi.mocked(casesApi.getComments).mockResolvedValue({ data: { comments: [] } } as never)
    vi.mocked(casesApi.addComment).mockResolvedValue({ data: {} } as never)
    render(<Comments caseId="case-2026-0142" />)

    fireEvent.change(await screen.findByPlaceholderText('Write a comment…'), {
      target: { value: 'Closing as benign' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Post/ }))

    await waitFor(() =>
      expect(casesApi.addComment).toHaveBeenCalledWith('case-2026-0142', {
        content: 'Closing as benign',
      }),
    )
    expect(vi.mocked(casesApi.addComment).mock.lastCall?.[1]).not.toHaveProperty('author')
  })

  it('adds evidence without collected_by', async () => {
    vi.mocked(casesApi.getEvidence).mockResolvedValue({ data: { evidence: [] } } as never)
    vi.mocked(casesApi.addEvidence).mockResolvedValue({ data: {} } as never)
    render(<EvidenceCard caseId="case-2026-0142" />)

    fireEvent.click(await screen.findByTitle('Add'))
    fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'pcap' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add evidence' }))

    await waitFor(() =>
      expect(casesApi.addEvidence).toHaveBeenCalledWith('case-2026-0142', {
        name: 'pcap',
        description: null,
        evidence_type: 'file',
        file_path: null,
      }),
    )
    expect(vi.mocked(casesApi.addEvidence).mock.lastCall?.[1]).not.toHaveProperty('collected_by')
  })
})

const denied = (detail: string) =>
  Object.assign(new Error('Request failed with status code 403'), { response: { status: 403, data: { detail } } })

describe('case section failures', () => {
  it('shows the server detail when evidence or IOCs fail to load', async () => {
    vi.mocked(casesApi.getEvidence).mockRejectedValue(denied('Not allowed to read evidence'))
    vi.mocked(casesApi.getIOCs).mockRejectedValue(new Error('Network Error'))
    render(<><EvidenceCard caseId="case-1" /><IOCsCard caseId="case-1" /></>)

    expect(await screen.findByText('Couldn’t load evidence: Not allowed to read evidence')).toBeInTheDocument()
    expect(await screen.findByText('Couldn’t load IOCs: Network Error')).toBeInTheDocument()
    expect(screen.queryByText('0 items')).not.toBeInTheDocument()
  })

  it('keeps the evidence form open and says why the write failed', async () => {
    vi.mocked(casesApi.getEvidence).mockResolvedValue({ data: { evidence: [] } } as never)
    vi.mocked(casesApi.addEvidence).mockRejectedValue(denied('Analyst role required'))
    render(<EvidenceCard caseId="case-1" />)

    fireEvent.click(await screen.findByTitle('Add'))
    fireEvent.change(screen.getByPlaceholderText('Name'), { target: { value: 'pcap' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add evidence' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Analyst role required')
    expect(screen.getByPlaceholderText('Name')).toHaveValue('pcap')
  })

  it('says why adding an IOC failed', async () => {
    vi.mocked(casesApi.getIOCs).mockResolvedValue({ data: { iocs: [] } } as never)
    vi.mocked(casesApi.addIOC).mockRejectedValue(denied('IOC already on this case'))
    render(<IOCsCard caseId="case-1" />)

    fireEvent.click(await screen.findByTitle('Add'))
    fireEvent.change(screen.getByPlaceholderText('Value'), { target: { value: '10.0.0.1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add IOC' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('IOC already on this case')
    expect(screen.getByPlaceholderText('Value')).toHaveValue('10.0.0.1')
  })

  it('says why adding or toggling a task failed, and clears it on the next success', async () => {
    vi.mocked(casesApi.addTask).mockRejectedValue(denied('Case is closed'))
    vi.mocked(casesApi.updateTask).mockRejectedValueOnce(new Error('Request failed with status code 500'))
    render(<Tasks caseId="case-1" />)

    fireEvent.click(await screen.findByTitle('Toggle'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Request failed with status code 500')

    fireEvent.click(screen.getByTitle('Add'))
    fireEvent.change(screen.getByPlaceholderText('Task title'), { target: { value: 'Isolate host' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add task' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Case is closed'))
    expect(screen.getByPlaceholderText('Task title')).toHaveValue('Isolate host')

    fireEvent.click(screen.getByTitle('Toggle'))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })

  it('keeps a comment that failed to post and shows the detail', async () => {
    vi.mocked(casesApi.getComments).mockResolvedValue({ data: { comments: [] } } as never)
    vi.mocked(casesApi.addComment).mockRejectedValue(denied('Comments are locked'))
    render(<Comments caseId="case-1" />)

    fireEvent.change(await screen.findByPlaceholderText('Write a comment…'), { target: { value: 'hello' } })
    fireEvent.click(screen.getByRole('button', { name: /Post/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Comments are locked')
    expect(screen.getByPlaceholderText('Write a comment…')).toHaveValue('hello')
  })
})
