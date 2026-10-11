import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { TasksView, useTasks } from './CaseSections'
import { casesApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  casesApi: {
    getTasks: vi.fn(),
    updateTask: vi.fn(),
    addTask: vi.fn(),
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
