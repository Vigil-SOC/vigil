/* The skill drawer: Save stays off until When to use it and Steps are both
   filled, each blank field says why, and a read-only file hides the Steps
   message without lifting the rule. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { SkillDrawer } from './SkillDrawer'
import { skillsApi } from '../../services/skillsApi'

vi.mock('../../services/skillsApi', () => ({
  skillsApi: {
    get: vi.fn(),
    file: vi.fn(() => Promise.resolve({ path: 'notes.md', content: 'Some notes.' })),
    save: vi.fn(() => Promise.resolve({})),
  },
}))

function openNew() {
  const onClose = vi.fn()
  const onSaved = vi.fn()
  render(<SkillDrawer name={null} existingNames={[]} onClose={onClose} onSaved={onSaved} />)
  return { onClose, onSaved, dialog: screen.getByRole('dialog', { name: 'Build a skill' }) }
}

describe('skill drawer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(skillsApi.save).mockResolvedValue({} as never)
    vi.mocked(skillsApi.file).mockResolvedValue({ path: 'notes.md', content: 'Some notes.' } as never)
  })

  it('keeps Save disabled with empty Steps and empty When to use it, showing both messages', async () => {
    const { dialog, onSaved } = openNew()
    const save = within(dialog).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    expect(within(dialog).getByText('Say when an agent should use this skill.')).toBeInTheDocument()
    expect(within(dialog).getByText('Add the steps an agent should follow.')).toBeInTheDocument()

    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'new-skill' } })
    fireEvent.change(within(dialog).getByLabelText('When to use it'), { target: { value: 'Does a thing.' } })
    expect(within(dialog).queryByText('Say when an agent should use this skill.')).toBeNull()
    expect(within(dialog).getByText('Add the steps an agent should follow.')).toBeInTheDocument()
    expect(save).toBeDisabled()

    // Whitespace alone is not steps.
    fireEvent.change(within(dialog).getByLabelText('Steps (SKILL.md)'), { target: { value: '   \n' } })
    expect(save).toBeDisabled()
    expect(within(dialog).getByText('Add the steps an agent should follow.')).toBeInTheDocument()

    fireEvent.change(within(dialog).getByLabelText('Steps (SKILL.md)'), { target: { value: '# Steps\n' } })
    expect(within(dialog).queryByText('Add the steps an agent should follow.')).toBeNull()
    expect(save).toBeEnabled()

    // Clearing When to use it turns Save back off, with its message back.
    fireEvent.change(within(dialog).getByLabelText('When to use it'), { target: { value: '  ' } })
    expect(save).toBeDisabled()
    expect(within(dialog).getByText('Say when an agent should use this skill.')).toBeInTheDocument()
    fireEvent.change(within(dialog).getByLabelText('When to use it'), { target: { value: 'Does a thing.' } })

    fireEvent.click(save)
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(skillsApi.save).toHaveBeenCalledWith({ name: 'new-skill', description: 'Does a thing.', body: '# Steps\n' })
  })

  it('hides the Steps message behind a read-only file but keeps Save disabled on an empty body', async () => {
    vi.mocked(skillsApi.get).mockResolvedValue({
      name: 'desk-check', description: 'A copy.', source_path: 'skills/desk-check', bundled: false,
      file_count: 2, body: '', operator_root_set: true, version: 1,
      files: [{ path: 'SKILL.md', size: 10 }, { path: 'notes.md', size: 5 }],
    } as never)
    render(<SkillDrawer name="desk-check" existingNames={['desk-check']} onClose={vi.fn()} onSaved={vi.fn()} />)
    const dialog = await screen.findByRole('dialog', { name: 'Edit desk-check' })
    expect(await within(dialog).findByLabelText('Steps (SKILL.md)')).toHaveValue('')
    expect(within(dialog).getByText('Add the steps an agent should follow.')).toBeInTheDocument()
    const save = within(dialog).getByRole('button', { name: 'Save new version' })
    expect(save).toBeDisabled()

    fireEvent.click(within(dialog).getByRole('button', { name: /notes\.md/ }))
    expect(await within(dialog).findByDisplayValue('Some notes.')).toBeInTheDocument()
    expect(within(dialog).queryByText('Add the steps an agent should follow.')).toBeNull()
    expect(save).toBeDisabled()
  })
})
