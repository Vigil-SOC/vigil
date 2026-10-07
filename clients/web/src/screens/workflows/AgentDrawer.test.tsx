/* The agent drawer: create and edit, each tool's mark, the two skills lines, load and save errors. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AgentDrawer } from './AgentDrawer'
import { agentsApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  agentsApi: {
    getCustom: vi.fn(),
    getAvailableTools: vi.fn(),
    createCustom: vi.fn(() => Promise.resolve({ data: {} })),
    updateCustom: vi.fn(() => Promise.resolve({ data: {} })),
    generateCustom: vi.fn(),
  },
  aiConfigApi: {
    listModels: vi.fn(() => Promise.resolve({ data: { models: [
      { model_id: 'claude-sonnet-5', display_name: 'Claude Sonnet 5' },
      { model_id: 'claude-haiku-4-5', display_name: 'Claude Haiku 4.5' },
    ] } })),
  },
}))

const CONNECTED = { search_logs: 'read_only', create_approval_action: 'asks_first', isolate_host: 'on_its_own', read_skill: 'read_only', get_alert: 'read_only' }
const SAVED = {
  id: 'custom-mine', name: 'Mine', specialization: 'Hunts', description: 'Hunts things', icon: 'M', color: '#112233',
  role: 'investigator', extra_principles: 'Be careful', methodology: '1. look', system_prompt_override: null,
  recommended_tools: ['search_logs', 'create_approval_action', 'isolate_host', 'okta_revoke_session'],
  max_tokens: 5000, enable_thinking: true, model: 'claude-old', fallback_model: null, effective_prompt: 'FULL PROMPT',
}

function open(props: Partial<React.ComponentProps<typeof AgentDrawer>> = {}) {
  const onClose = vi.fn()
  const onSaved = vi.fn()
  render(<AgentDrawer agentId="custom-mine" onClose={onClose} onSaved={onSaved} {...props} />)
  return { onClose, onSaved }
}

describe('agent drawer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(agentsApi.getCustom).mockResolvedValue({ data: SAVED } as never)
    vi.mocked(agentsApi.getAvailableTools).mockResolvedValue({ data: { tools: [], changes: CONNECTED } } as never)
  })

  it('creates a blank agent with the defaults and null models', async () => {
    const { onSaved } = open({ agentId: null })
    expect(screen.getByText('New agent')).toBeInTheDocument()
    const create = screen.getByRole('button', { name: 'Create agent' })
    expect(create).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Fresh' } })
    fireEvent.change(screen.getByLabelText('Role'), { target: { value: 'hunter' } })
    fireEvent.click(create)
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(agentsApi.createCustom).toHaveBeenCalledWith(expect.objectContaining({
      name: 'Fresh', role: 'hunter', model: null, fallback_model: null, max_tokens: 4096, enable_thinking: false, color: '#7d74f3', icon: null,
    }))
  })

  it('edits a custom agent: stored icon and colour are kept, an unlisted model and size stay options', async () => {
    const { onSaved } = open()
    expect(await screen.findByText('Edit agent · Mine')).toBeInTheDocument()
    expect(await screen.findByLabelText('Name')).toHaveValue('Mine')
    expect(screen.getByLabelText('Model')).toHaveValue('claude-old')
    expect(within(screen.getByLabelText('Model')).getByRole('option', { name: 'Use the default' })).toBeInTheDocument()
    expect(within(screen.getByLabelText('If it is unavailable, use')).getByRole('option', { name: 'No fallback' })).toBeInTheDocument()
    expect(screen.getByLabelText('Longest answer')).toHaveValue('5000')
    expect(screen.getByLabelText('Thinking')).toHaveValue('on')
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(agentsApi.updateCustom).toHaveBeenCalledWith('custom-mine', expect.objectContaining({
      icon: 'M', color: '#112233', model: null, max_tokens: 5000, enable_thinking: true,
      recommended_tools: SAVED.recommended_tools, system_prompt_override: null,
    }))
  })

  it('marks each tool, and a tool the list does not know as not connected', async () => {
    open()
    const rowOf = async (tool: string) => (await screen.findByText(tool)).closest('.vg-side-tool') as HTMLElement
    expect(await rowOf('search_logs')).toHaveTextContent('On its own')
    expect(await rowOf('search_logs')).not.toHaveTextContent('changes things')
    expect(await rowOf('create_approval_action')).toHaveTextContent('Asks you')
    expect(await rowOf('isolate_host')).toHaveTextContent('On its own changes things')
    expect(await rowOf('okta_revoke_session')).toHaveTextContent('Not connected')
  })

  it('adds a connected tool, removes one, and offers no free text', async () => {
    open()
    await screen.findByText('search_logs')
    fireEvent.click(screen.getByRole('button', { name: 'Remove search_logs' }))
    expect(screen.queryByRole('button', { name: 'Remove search_logs' })).toBeNull()
    await waitFor(() => expect(within(screen.getByLabelText('Add tool')).getByRole('option', { name: 'get_alert' })).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Add tool'), { target: { value: 'get_alert' } })
    expect((await screen.findByText('get_alert')).closest('.vg-side-tool')).toHaveTextContent('On its own')
    expect(screen.queryByRole('textbox', { name: /tools/i })).toBeNull()
  })

  it('says which skills it is offered', async () => {
    open({ skillCount: 12 })
    await screen.findByText('search_logs')
    expect(screen.getByText('Not offered skills.')).toBeInTheDocument()
    await waitFor(() => expect(within(screen.getByLabelText('Add tool')).getByRole('option', { name: 'read_skill' })).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Add tool'), { target: { value: 'read_skill' } })
    expect(screen.getByText('Offered every skill in the library (12). Choosing single skills comes later.')).toBeInTheDocument()
  })

  it('shows the row’s marks at once, before the connected list answers', async () => {
    vi.mocked(agentsApi.getAvailableTools).mockReturnValue(new Promise(() => {}))
    open({ toolChanges: { search_logs: 'read_only', isolate_host: 'on_its_own' } })
    expect((await screen.findByText('isolate_host')).closest('.vg-side-tool')).toHaveTextContent('changes things')
    expect(screen.getByText('okta_revoke_session').closest('.vg-side-tool')).not.toHaveTextContent('Not connected')
  })

  it('reveals the override box and the prompt preview under Advanced', async () => {
    open()
    await screen.findByText('search_logs')
    expect(screen.queryByLabelText(/Whole prompt/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Advanced' }))
    expect(screen.getByLabelText(/Whole prompt/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Preview effective prompt' }))
    expect(screen.getByText('FULL PROMPT')).toBeInTheDocument()
  })

  it('shows a load error and no form', async () => {
    vi.mocked(agentsApi.getCustom).mockRejectedValue(new Error('nope'))
    open()
    expect(await screen.findByText(/Couldn’t load agent: nope/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Name')).toBeNull()
  })

  it('keeps the drawer open and shows the error when a save fails', async () => {
    vi.mocked(agentsApi.updateCustom).mockRejectedValue(new Error('name taken'))
    const { onSaved } = open()
    await screen.findByText('search_logs')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('name taken')).toBeInTheDocument()
    expect(onSaved).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('closes on Escape and on the scrim, not on a click inside', async () => {
    const { onClose } = open()
    await screen.findByText('search_logs')
    fireEvent.mouseDown(screen.getByRole('dialog'))
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.mouseDown(screen.getByRole('dialog').parentElement as HTMLElement)
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('drafts a new agent from a description and applies its icon and colour', async () => {
    const draft = { name: 'Cloud IAM', description: 'd', specialization: 's', icon: 'C', color: '#abcdef', role: 'cloud triager', extra_principles: '', methodology: '', recommended_tools: ['search_logs'], max_tokens: 8192, enable_thinking: false }
    vi.mocked(agentsApi.generateCustom).mockResolvedValue({ data: { draft } } as never)
    open({ agentId: null, describe: true })
    fireEvent.change(screen.getByLabelText('Describe the agent'), { target: { value: 'IAM triage' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate draft' }))
    await waitFor(() => expect(screen.getByLabelText('Role')).toHaveValue('cloud triager'))
    expect(agentsApi.generateCustom).toHaveBeenCalledWith({ description: 'IAM triage', current_draft: null, feedback: undefined })
    fireEvent.click(screen.getByRole('button', { name: 'Create agent' }))
    await waitFor(() => expect(agentsApi.createCustom).toHaveBeenCalledWith(expect.objectContaining({ icon: 'C', color: '#abcdef', max_tokens: 8192 })))
  })

  it('drafts a change to an agent with the current draft and the feedback, keeping icon and colour', async () => {
    const draft = { name: 'Mine', description: 'Hunts things', specialization: 'Hunts', icon: 'Z', color: '#ffffff', role: 'careful investigator', extra_principles: 'Be careful', methodology: '1. look', recommended_tools: ['search_logs'], max_tokens: 5000, enable_thinking: true }
    vi.mocked(agentsApi.generateCustom).mockResolvedValue({ data: { draft } } as never)
    open()
    await screen.findByText('search_logs')
    fireEvent.change(screen.getByLabelText(/Describe a change/), { target: { value: 'be more careful with finance hosts' } })
    fireEvent.click(screen.getByRole('button', { name: 'Draft change' }))
    await waitFor(() => expect(screen.getByLabelText('Role')).toHaveValue('careful investigator'))
    expect(agentsApi.generateCustom).toHaveBeenCalledWith(expect.objectContaining({
      description: 'Hunts things', feedback: 'be more careful with finance hosts', current_draft: expect.objectContaining({ role: 'investigator', icon: 'M' }),
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(agentsApi.updateCustom).toHaveBeenCalledWith('custom-mine', expect.objectContaining({ icon: 'M', color: '#112233' })))
  })
})
