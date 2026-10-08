import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { SkillDrawer } from './SkillDrawer'

const h = vi.hoisted(() => ({ get: vi.fn(), test: vi.fn() }))
vi.mock('../../services/skillsApi', () => ({ skillsApi: { get: h.get, test: h.test } }))

const detail = {
  name: 'phishing-triage', description: 'Triage a reported email', source_path: '/x', bundled: true,
  body: '# Steps', operator_root_set: true, version: 1, files: [],
}

const open = (name: string | null = 'phishing-triage') =>
  render(<SkillDrawer name={name} existingNames={[]} onClose={vi.fn()} onSaved={vi.fn()} />)

describe('SkillDrawer: Test with a sample', () => {
  beforeEach(() => {
    h.get.mockReset().mockResolvedValue(detail)
    h.test.mockReset()
  })

  it('is absent when building a new skill', async () => {
    open(null)
    await screen.findByLabelText('Name')
    expect(screen.queryByText('Test with a sample')).toBeNull()
  })

  it('disables the button while running, then lists each case with the missing strings', async () => {
    let finish!: (v: unknown) => void
    h.test.mockReturnValue(new Promise((r) => { finish = r }))
    open()
    fireEvent.click(await screen.findByRole('button', { name: 'Test with a sample' }))
    expect(h.test).toHaveBeenCalledWith('phishing-triage')
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
    h.test.mockResolvedValue({ no_cases: true, model: null, results: [] })
    open()
    fireEvent.click(await screen.findByRole('button', { name: 'Test with a sample' }))
    expect(await screen.findByText('This skill has no test cases.')).toBeInTheDocument()
  })

  it('shows the server detail when the run fails', async () => {
    h.test.mockRejectedValue({ response: { data: { detail: 'No LLM provider is configured.' } } })
    open()
    fireEvent.click(await screen.findByRole('button', { name: 'Test with a sample' }))
    await waitFor(() => expect(screen.getByText('No LLM provider is configured.')).toBeInTheDocument())
  })
})
