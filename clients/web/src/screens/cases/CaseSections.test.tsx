import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { CommentsView, EvidenceCard, TasksView, useComments, useTasks } from './CaseSections'
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
