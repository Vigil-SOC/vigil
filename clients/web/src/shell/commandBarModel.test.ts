import { describe, expect, it } from 'vitest'
import { attachRefusal, commandPreview, huntTitle, proposalFrom, type HuntAttachment, type HuntProposal } from './commandBarModel'

describe('huntTitle', () => {
  it('keeps a short hypothesis whole', () => {
    expect(huntTitle('  rare   beacon ')).toBe('rare beacon')
  })

  it('ends on a whole word, within the limit', () => {
    const text = 'a service account key was used from a new network and then read customer-exports'
    const title = huntTitle(text)
    expect(title).toBe('a service account key was used from a new network and then')
    expect(title.length).toBeLessThanOrEqual(60)
    expect(text.startsWith(`${title} `)).toBe(true)
  })

  it('keeps a word that ends exactly at the limit, and drops trailing punctuation', () => {
    expect(huntTitle('alpha beta, gamma delta', 11)).toBe('alpha beta')
    expect(huntTitle('alpha beta gamma', 10)).toBe('alpha beta')
  })

  it('hard-cuts a single word longer than the limit', () => {
    expect(huntTitle('x'.repeat(100), 20)).toBe('x'.repeat(20))
  })
})

const JIRA = { gap: null, projectKey: '' }
const READY = (proposal: HuntProposal | null): HuntAttachment => ({
  status: 'ready', name: 'a.pdf', file: new File([''], 'a.pdf'), pasted: false, pages: 2, condensed: false, text: 'r', proposal,
})

describe('attachRefusal', () => {
  it('names a wrong type and an over-size file, and lets the rest through', () => {
    expect(attachRefusal({ name: 'tool.exe', size: 10 })).toMatch(/\.exe files cannot be attached/)
    expect(attachRefusal({ name: 'big.pdf', size: 26 * 1024 * 1024 })).toMatch(/25 MB/)
    expect(attachRefusal({ name: 'Brief.PDF', size: 25 * 1024 * 1024 })).toBeNull()
  })
})

describe('/hunt preview with an attachment', () => {
  it('is not "Add a hypothesis" once a document proposes one', () => {
    const view = commandPreview('hunt', '', JIRA, READY({ status: 'proposed', hypothesis: 'H1' }))
    expect(view).toMatchObject({ line: 'Start a threat hunt: H1', disabled: false })
    expect(commandPreview('hunt', 'mine', JIRA, READY({ status: 'proposed', hypothesis: 'H1' })).line).toBe('Start a threat hunt: mine')
  })

  it('stays disabled while reading, when refused, and with no proposal', () => {
    expect(commandPreview('hunt', 'x', JIRA, { status: 'reading', name: 'a.pdf' }).disabled).toBe(true)
    expect(commandPreview('hunt', 'x', JIRA, { status: 'refused', name: 'a.pdf', reason: 'No' })).toEqual({ line: 'No', disabled: true })
    const none = proposalFrom({ status: 'running' })
    expect(commandPreview('hunt', '', JIRA, READY(none)).disabled).toBe(true)
    expect(commandPreview('hunt', '', JIRA).line).toBe('Add a hypothesis')
  })
})
