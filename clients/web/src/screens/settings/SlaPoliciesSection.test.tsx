import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import SlaPoliciesSection from './SlaPoliciesSection'

const api = vi.hoisted(() => ({
  getAll: vi.fn(),
  getUsage: vi.fn(),
  create: vi.fn(),
  update: vi.fn(),
  delete: vi.fn(),
}))
vi.mock('../../services/api', () => ({ slaPoliciesApi: api }))

const HIGH = {
  policy_id: 'sla-high',
  name: 'High policy',
  priority_level: 'high',
  response_time_hours: 2,
  resolution_time_hours: 8,
  business_hours_only: false,
  notification_thresholds: [50, 75, 90],
  is_active: true,
  is_default: true,
}
const LOW = { ...HIGH, policy_id: 'sla-low', name: 'Low policy', priority_level: 'low', is_default: false, business_hours_only: true, notification_thresholds: [90] }

const usageFor: Record<string, unknown> = {
  'sla-high': { total_cases: 38, breached_cases: 2, compliance_rate: 94.74 },
  'sla-low': { total_cases: 0, breached_cases: 0, compliance_rate: 0 },
}

const notify = vi.fn()
const renderSection = () => render(<SlaPoliciesSection notify={notify} />)

beforeEach(() => {
  vi.clearAllMocks()
  api.getAll.mockResolvedValue({ data: [HIGH, LOW] })
  api.getUsage.mockImplementation((id: string) => Promise.resolve({ data: usageFor[id] }))
  api.create.mockResolvedValue({})
  api.update.mockResolvedValue({})
  api.delete.mockResolvedValue({})
})

describe('SlaPoliciesSection states', () => {
  it('loading', () => {
    api.getAll.mockReturnValue(new Promise(() => {}))
    renderSection()
    expect(screen.getByText('Loading SLA policies…')).toBeInTheDocument()
  })

  it('error offers Retry, which reloads', async () => {
    api.getAll.mockRejectedValueOnce(new Error('boom'))
    renderSection()
    expect(await screen.findByText(/Couldn’t load SLA policies: boom/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('High policy')).toBeInTheDocument()
  })

  it('empty', async () => {
    api.getAll.mockResolvedValue({ data: [] })
    renderSection()
    expect(await screen.findByText('No SLA policies yet.')).toBeInTheDocument()
  })

  it('populated: the board columns, with Met read off the count and the month window', async () => {
    renderSection()
    const row = (await screen.findByText('High policy')).closest('tr')!
    expect(within(row).getByText('2 hours')).toBeInTheDocument()
    expect(within(row).getByText('8 hours')).toBeInTheDocument()
    expect(within(row).getByText('Around the clock')).toBeInTheDocument()
    expect(within(row).getByText('50%, 75%, 90%')).toBeInTheDocument()
    await waitFor(() => expect(within(row).getByText('95%')).toBeInTheDocument())
    expect(within(row).getByText('38 cases this month')).toBeInTheDocument()

    const empty = screen.getByText('Low policy').closest('tr')!
    expect(within(empty).getByText('Business hours')).toBeInTheDocument()
    expect(within(empty).getByText('Low').closest('.sev-mark')).toHaveClass('low')
    await waitFor(() => expect(within(empty).getByText('Not measured yet')).toBeInTheDocument())
    expect(within(empty).queryByText('0%')).not.toBeInTheDocument()

    const since = api.getUsage.mock.calls[0][1].since as string
    expect(since).toMatch(/^\d{4}-\d{2}-01T00:00:00\.000Z$/)
    expect(screen.queryByText(/Timers pause/)).not.toBeInTheDocument()
    const head = document.querySelector('.page-head') as HTMLElement
    expect(within(head).getByRole('heading', { name: 'SLA policies' })).toBeInTheDocument()
    expect(within(head).getByRole('button', { name: /New policy/ })).toBeInTheDocument()
  })
})

describe('SlaPoliciesSection editor', () => {
  const openEdit = async () => {
    renderSection()
    await screen.findByText('High policy')
    fireEvent.click(screen.getByRole('button', { name: 'Edit High policy' }))
  }

  it('edits in place: stored warn-at is read-only and is not sent back', async () => {
    await openEdit()
    expect(screen.getByRole('heading', { name: 'Editing: High severity' })).toBeInTheDocument()
    const panel = screen.getByRole('region', { name: 'Edit policy' })
    expect(within(panel).getByText('50%, 75%, 90%')).toBeInTheDocument()
    expect(within(panel).getByText('Later')).toBeInTheDocument()
    expect(within(panel).queryByRole('button', { name: /Add escalation/ })).not.toBeInTheDocument()

    fireEvent.click(within(panel).getByRole('button', { name: 'Around the clock' }))
    fireEvent.click(await screen.findByRole('option', { name: /Business hours/ }))
    fireEvent.click(within(panel).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.update).toHaveBeenCalled())
    const [id, body] = api.update.mock.calls[0]
    expect(id).toBe('sla-high')
    expect(body).toMatchObject({ business_hours_only: true, response_time_hours: 2, resolution_time_hours: 8 })
    expect(body).not.toHaveProperty('notification_thresholds')
  })

  it('Refresh keeps the editor open', async () => {
    await openEdit()
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await waitFor(() => expect(api.getAll).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('heading', { name: 'Editing: High severity' })).toBeInTheDocument()
  })

  it('refuses resolve equal to respond, without calling the server', async () => {
    await openEdit()
    const panel = screen.getByRole('region', { name: 'Edit policy' })
    fireEvent.click(within(panel).getByRole('button', { name: /^Resolve within:/ }))
    fireEvent.click(within(panel).getByRole('button', { name: '1 h' })) // 1 h < 2 h respond
    fireEvent.click(within(panel).getByRole('button', { name: 'Save' }))
    expect(await within(panel).findByText(/must be longer/)).toBeInTheDocument()
    expect(api.update).not.toHaveBeenCalled()
  })

  it('creates with the same panel, and sends no thresholds', async () => {
    renderSection()
    await screen.findByText('High policy')
    fireEvent.click(screen.getByRole('button', { name: /New policy/ }))
    const panel = screen.getByRole('region', { name: 'Edit policy' })
    fireEvent.change(within(panel).getByRole('textbox', { name: /Name/ }), { target: { value: 'My Policy' } })
    fireEvent.click(within(panel).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(api.create).toHaveBeenCalled())
    const body = api.create.mock.calls[0][0]
    expect(body).toMatchObject({ policy_id: 'my-policy', priority_level: 'high', business_hours_only: false, response_time_hours: 4, resolution_time_hours: 24 })
    expect(body).not.toHaveProperty('notification_thresholds')
  })

  it('deletes through the confirm dialog', async () => {
    await openEdit()
    fireEvent.click(screen.getByRole('button', { name: /Delete/ }))
    expect(api.delete).not.toHaveBeenCalled()
    const buttons = await screen.findAllByRole('button', { name: 'Delete' })
    fireEvent.click(buttons[buttons.length - 1]) // the dialog's, rendered last
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('sla-high'))
  })
})
