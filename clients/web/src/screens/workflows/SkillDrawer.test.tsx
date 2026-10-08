/* The skill drawer: Save needs a name, when-to-use and steps, and each blank field says why. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { SkillDrawer } from './SkillDrawer'
import { skillsApi } from '../../services/skillsApi'

vi.mock('../../services/skillsApi', () => ({
  skillsApi: {
    get: vi.fn(() => Promise.resolve({ operator_root_set: true })),
    file: vi.fn(),
    save: vi.fn(() => Promise.resolve({})),
  },
}))

const STEPS = 'Add the steps an agent should follow.'
const WHEN = 'Describe when an agent should use this skill.'

describe('skill drawer', () => {
  beforeEach(() => vi.clearAllMocks())

  it('keeps Save off, and says why, until both when-to-use and steps are filled', async () => {
    const onSaved = vi.fn()
    render(<SkillDrawer name={null} existingNames={[]} onClose={vi.fn()} onSaved={onSaved} />)
    const save = screen.getByRole('button', { name: 'Save' })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'my-skill' } })
    expect(save).toBeDisabled()
    expect(screen.getByText(WHEN)).toBeInTheDocument()
    expect(screen.getByText(STEPS)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('When to use it'), { target: { value: 'When triaging.' } })
    fireEvent.change(screen.getByLabelText('Steps (SKILL.md)'), { target: { value: '  \n ' } })
    expect(save).toBeDisabled()
    expect(screen.queryByText(WHEN)).not.toBeInTheDocument()
    expect(screen.getByText(STEPS)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Steps (SKILL.md)'), { target: { value: '1. Look.' } })
    expect(screen.queryByText(STEPS)).not.toBeInTheDocument()
    expect(save).toBeEnabled()
    fireEvent.click(save)
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(skillsApi.save).toHaveBeenCalledWith(expect.objectContaining({ name: 'my-skill', body: '1. Look.' }))
  })
})
