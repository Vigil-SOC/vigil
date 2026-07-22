import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import CaseCreateDialog from './CaseCreateDialog'
import { casesApi } from '../../../services/api'

vi.mock('../../../services/api', () => ({
  casesApi: { create: vi.fn() },
}))

const createdResponse = (caseId: string) => ({ data: { case_id: caseId } }) as never

beforeEach(() => {
  vi.mocked(casesApi.create).mockReset()
})

describe('CaseCreateDialog', () => {
  it('creates a standalone case without linked findings', async () => {
    vi.mocked(casesApi.create).mockResolvedValueOnce(createdResponse('case-standalone'))
    const onCreated = vi.fn()
    const onClose = vi.fn()
    render(<CaseCreateDialog open onClose={onClose} onCreated={onCreated} />)

    fireEvent.change(screen.getByRole('textbox', { name: 'Title' }), { target: { value: 'Standalone review' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create case' }))

    await waitFor(() => expect(casesApi.create).toHaveBeenCalledWith({
      title: 'Standalone review',
      description: undefined,
      finding_ids: [],
      priority: 'medium',
      status: 'open',
    }))
    expect(onCreated).toHaveBeenCalledWith('case-standalone')
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('deduplicates findings and defaults to the highest selected severity', async () => {
    vi.mocked(casesApi.create).mockResolvedValueOnce(createdResponse('case-linked'))
    render(
      <CaseCreateDialog
        open
        initialFindings={[
          { id: 'finding-1', severity: 'low' },
          { id: 'finding-2', severity: 'critical' },
          { id: 'finding-1', severity: 'high' },
        ]}
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    )

    expect(screen.getByRole('textbox', { name: 'Title' })).toHaveValue('Investigation for 2 findings')
    const linked = within(screen.getByLabelText('Linked findings'))
    expect(linked.getAllByText('finding-1')).toHaveLength(1)
    expect(linked.getAllByText('finding-2')).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Create case' }))
    await waitFor(() => expect(casesApi.create).toHaveBeenCalledWith({
      title: 'Investigation for 2 findings',
      description: undefined,
      finding_ids: ['finding-1', 'finding-2'],
      priority: 'critical',
      status: 'open',
    }))
  })

  it('uses the finding identifier for a single linked case', () => {
    render(
      <CaseCreateDialog
        open
        initialFindings={[{ id: 'finding-42', severity: 'high' }]}
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    )

    expect(screen.getByRole('textbox', { name: 'Title' })).toHaveValue('Investigation for finding-42')
    expect(screen.getByRole('button', { name: 'Priority' })).toHaveTextContent('High')
  })

  it('cancels without submitting', () => {
    const onClose = vi.fn()
    render(<CaseCreateDialog open onClose={onClose} onCreated={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(casesApi.create).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('keeps the dialog open and reports backend failures', async () => {
    vi.mocked(casesApi.create).mockRejectedValueOnce({
      response: { data: { detail: 'One or more findings no longer exist' } },
    })
    const onClose = vi.fn()
    render(
      <CaseCreateDialog
        open
        initialFindings={[{ id: 'finding-1', severity: 'medium' }]}
        onClose={onClose}
        onCreated={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Create case' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('One or more findings no longer exist')
    expect(screen.getByRole('dialog', { name: 'Create case from findings' })).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })
})
