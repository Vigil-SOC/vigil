import { describe, expect, it } from 'vitest'
import { attachRefusal, buildRows, commandPreview, huntTitle, proposalFrom, type HuntAttachment, type HuntProposal } from './commandBarModel'

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

  it('proposes beside a hunt already running and names it', () => {
    const answer = { status: 'running', in_flight: [{ run_id: 'r1', case_id: 'c1' }], proposal: { hypothesis: 'H1' } }
    const proposal = proposalFrom(answer)
    expect(proposal).toMatchObject({ status: 'proposed', hypothesis: 'H1', running: { caseId: 'c1', runId: 'r1' } })
    expect(commandPreview('hunt', '', JIRA, READY(proposal))).toMatchObject({ disabled: false, note: expect.stringContaining('A hunt on this document is') })
  })

  it('names the hunt\'s real state: paused is not running', () => {
    const note = (status: string | undefined, caseId: string | null = 'c1') =>
      commandPreview('hunt', '', JIRA, READY(proposalFrom({ status: 'running', in_flight: [{ run_id: 'r1', case_id: caseId, status }], proposal: { hypothesis: 'H1' } }))).note
    expect(note('paused')).toBe('A hunt on this document is paused (case c1). Run starts a new one.')
    expect(note('running')).toBe('A hunt on this document is running (case c1). Run starts a new one.')
    expect(note(undefined, null)).toBe('A hunt on this document is in progress. Run starts a new one.')
  })

  it('names the live hunt when a newer paused one leads', () => {
    const proposal = proposalFrom({
      status: 'running',
      in_flight: [{ run_id: 'r2', case_id: 'c2', status: 'paused' }, { run_id: 'r1', case_id: 'c1', status: 'running' }],
      proposal: { hypothesis: 'H1' },
    })
    expect(proposal).toMatchObject({ running: { caseId: 'c1', status: 'running' } })
  })

  it('says paused, not running, when the only hunt is paused and nothing is proposed', () => {
    const none = proposalFrom({ status: 'running', in_flight: [{ run_id: 'r1', status: 'paused' }] })
    expect(none).toMatchObject({ status: 'none', reason: expect.stringContaining('is paused') })
  })

  it('stays disabled while reading, when refused, and with no proposal', () => {
    expect(commandPreview('hunt', 'x', JIRA, { status: 'reading', name: 'a.pdf' }).disabled).toBe(true)
    expect(commandPreview('hunt', 'x', JIRA, { status: 'refused', name: 'a.pdf', reason: 'No' })).toEqual({ line: 'No', disabled: true })
    const none = proposalFrom({ status: 'uncovered' })
    expect(commandPreview('hunt', '', JIRA, READY(none)).disabled).toBe(true)
    expect(commandPreview('hunt', '', JIRA).line).toBe('Add a hypothesis')
  })
})

describe('buildRows alert hits', () => {
  it('puts entity-matched alerts after the cases and does not repeat the exact id', () => {
    const rows = buildRows('fin-ws-114', {
      cases: [{ id: 'case-7', title: 'Finance hunt', match_type: 'alert' }],
      alerts: [{ id: 'f-7', title: 'Beacon' }, { id: 'f-8', title: 'Logon' }],
    }, [], [])
    expect(rows.map((row) => [row.section, row.hint])).toEqual([
      ['Cases', 'case-7'],
      ['Alerts', 'f-7'],
      ['Alerts', 'f-8'],
      ['Ask Vigil', ''],
    ])
    expect(rows[0].desc).toBe('Matched an alert')

    const exact = buildRows('f-7', {
      finding_exact: { id: 'f-7', title: 'Beacon' },
      alerts: [{ id: 'f-7', title: 'Beacon' }],
    }, [], [])
    expect(exact.filter((row) => row.findingId === 'f-7')).toHaveLength(1)
  })
})

describe('buildRows workflows, agents and skills', () => {
  it('lists them after the pages, each opening its place in Agents & workflows', () => {
    const rows = buildRows('phishing', {
      workflows: [{ id: 'phishing-response', title: 'Phishing response', description: 'Contain a reported lure' }],
      agents: [{ id: 'custom-mail cop', title: 'Mail cop' }],
      skills: [{ id: 'phishing-triage', title: 'phishing-triage', description: 'Give a reported email a verdict' }],
    }, [], [{ key: 'triage', label: 'Phishing queue', icon: 'grid', desc: 'Queue' }])
    expect(rows.map((row) => [row.section, row.label, row.desc, row.page, row.search])).toEqual([
      ['Pages', 'Phishing queue', 'Queue', 'triage', undefined],
      ['Agents & workflows', 'Phishing response', 'Workflow · Contain a reported lure', 'workflows', '?workflow=phishing-response'],
      ['Agents & workflows', 'Mail cop', 'Agent', 'workflows', '?tab=agents&agent=custom-mail%20cop'],
      ['Agents & workflows', 'phishing-triage', 'Skill · Give a reported email a verdict', 'workflows', '?tab=skills&skill=phishing-triage'],
      ['Ask Vigil', 'Ask Vigil: “phishing”', 'Opens chat with your question', undefined, undefined],
    ])
    expect(rows.slice(1, 4).map((row) => row.dest)).toEqual(['Page', 'Page', 'Page'])
  })
})
