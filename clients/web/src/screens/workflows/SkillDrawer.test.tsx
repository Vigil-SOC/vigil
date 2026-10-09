import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { SkillDrawer } from './SkillDrawer'
import { skillsApi } from '../../services/skillsApi'

vi.mock('../../services/skillsApi', () => ({
  skillsApi: {
    get: vi.fn(),
    file: vi.fn(),
    save: vi.fn(() => Promise.resolve({})),
    test: vi.fn(),
  },
}))

const STEPS = 'Add the steps an agent should follow.'
const WHEN = 'Describe when an agent should use this skill.'

const detail = {
  name: 'phishing-triage', description: 'Triage a reported email', source_path: '/x', bundled: true,
  body: '# Steps', operator_root_set: true, version: 1, files: [],
}

const open = (name: string | null = 'phishing-triage') =>
  render(<SkillDrawer name={name} existingNames={[]} onClose={vi.fn()} onSaved={vi.fn()} />)

describe('skill drawer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(skillsApi.get).mockResolvedValue(detail as never)
  })

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

  describe('Test with a sample', () => {
    it('is absent when building a new skill', async () => {
      open(null)
      await screen.findByLabelText('Name')
      expect(screen.queryByText('Test with a sample')).toBeNull()
    })

    it('disables the button while running, then lists each case with the missing strings', async () => {
      let finish!: (v: unknown) => void
      vi.mocked(skillsApi.test).mockReturnValue(new Promise((r) => { finish = r }) as never)
      open()
      fireEvent.click(await screen.findByRole('button', { name: 'Test with a sample' }))
      expect(skillsApi.test).toHaveBeenCalledWith('phishing-triage')
      expect(screen.getByRole('button', { name: 'Testing…' })).toBeDisabled()
      finish({
        no_cases: false, model: 'qwen3:8b',
        results: [
          { name: 'look-alike domain', passed: true, missing: [] },
          { name: 'benign newsletter', passed: false, missing: ['VERDICT: BENIGN', 'T1566'] },
        ],
      })
      expect(await screen.findByText('1 of 2 passed', { exact: false })).toBeInTheDocument()
      expect(screen.getByText('benign newsletter')).toBeInTheDocument()
      expect(screen.getByText('Missing: VERDICT: BENIGN · T1566')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Test with a sample' })).toBeEnabled()
    })

    it('says when the skill has no cases', async () => {
      vi.mocked(skillsApi.test).mockResolvedValue({ no_cases: true, model: null, results: [] } as never)
      open()
      fireEvent.click(await screen.findByRole('button', { name: 'Test with a sample' }))
      expect(await screen.findByText('This skill has no test cases.')).toBeInTheDocument()
    })

    it('shows the server detail when the run fails', async () => {
      vi.mocked(skillsApi.test).mockRejectedValue({ response: { data: { detail: 'No LLM provider is configured.' } } })
      open()
      fireEvent.click(await screen.findByRole('button', { name: 'Test with a sample' }))
      await waitFor(() => expect(screen.getByText('No LLM provider is configured.')).toBeInTheDocument())
    })
  })
})
