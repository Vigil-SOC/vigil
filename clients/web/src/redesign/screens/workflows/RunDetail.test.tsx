/* The hunt panel. Everything asserted here is data the projection already
   carried and the console used to throw away: gaps, checkpoints, escalations
   and the report itself were reachable only as prose, and only after terminal. */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { RunDetail } from './WorkflowsScreen'

vi.mock('../../../services/api', () => ({
  workflowApi: {
    steer: vi.fn(() => Promise.resolve({ data: {} })),
    cancelRun: vi.fn(() => Promise.resolve({ data: {} })),
  },
  agentsApi: { listAgents: vi.fn(() => Promise.resolve({ data: { agents: [] } })) },
  findingsApi: { getAll: vi.fn(() => Promise.resolve({ data: { findings: [] } })) },
  casesApi: { getAll: vi.fn(() => Promise.resolve({ data: { cases: [] } })) },
}))
vi.mock('../../../services/skillsApi', () => ({
  skillsApi: { getAll: vi.fn(() => Promise.resolve({ data: { skills: [] } })) },
  SKILL_CATEGORIES: [],
}))

const hunt = (over = {}) => ({
  status: 'terminal',
  iteration: 5,
  evidence_count: 34,
  cost_usd: 4.1786,
  hypotheses: [
    {
      hypothesis_id: 'h-3431',
      statement: 'An internal host is beaconing on a regular interval',
      status: 'handed_off',
      attack_technique: null,
      techniques_cited: ['T1071.001'],
      resolution_reason: 'survived the argue-the-null pass',
      provenance: 'hunt_spec',
    },
  ],
  ...over,
})

const detail = (over = {}) => ({ run_id: 'run-1', status: 'completed', ...over })

const renderPanel = (over = {}) =>
  render(<RunDetail d={detail(over)} onSteered={() => {}} />)

/** The panel is four views of one run rather than five stacked tables, so a test
 *  about one of them says which. A tab that is not offered at all is a claim in
 *  itself and getByRole fails loudly rather than silently finding nothing. */
const tabTo = (name: RegExp) => fireEvent.click(screen.getByRole('tab', { name }))

describe('what a finished hunt shows an operator', () => {
  it('names each belief, the techniques its evidence cited and where it stands', () => {
    renderPanel({ hunt: hunt() })

    expect(screen.getByText(/An internal host is beaconing/)).toBeInTheDocument()
    expect(screen.getByText('T1071.001')).toBeInTheDocument()
    expect(screen.getByText('handed_off')).toBeInTheDocument()
  })

  it('lists every technique the evidence cited, not just the first', () => {
    renderPanel({ hunt: hunt({ hypotheses: [{ hypothesis_id: 'h-1', statement: 'beaconing', status: 'active', techniques_cited: ['T1071.001', 'T1496'] }] }) })

    expect(screen.getByText('T1071.001, T1496')).toBeInTheDocument()
  })

  // A run journaled before the vocabulary and the label were separated carries a
  // declared technique and no citations, and still has to read correctly.
  it('falls back to a technique an older run declared', () => {
    renderPanel({ hunt: hunt({ hypotheses: [{ hypothesis_id: 'h-1', statement: 'beaconing', status: 'active', attack_technique: 'T1078' }] }) })

    expect(screen.getByText('T1078')).toBeInTheDocument()
  })

  it('says nothing rather than inventing a technique for an uncited belief', () => {
    renderPanel({ hunt: hunt({ hypotheses: [{ hypothesis_id: 'h-1', statement: 'beaconing', status: 'active', techniques_cited: [] }] }) })

    expect(screen.getByText('—')).toBeInTheDocument()
  })

  // The screen a real run left behind: nine beliefs, every column identical, because
  // every informative field on a standing is written at verdict time and nothing
  // resolved. The rulings are on the ledger the whole time.
  describe('how the evidence landed', () => {
    const board = {
      evidence_count: 3,
      hypotheses: [
        { hypothesis_id: 'h-c2', statement: 'this is attacker command-and-control', status: 'inconclusive', techniques_cited: ['T1071.001'], resolution_reason: 'hunt ended (budget_terminated) with the hypothesis unresolved', provenance: 'operator' },
        { hypothesis_id: 'h-mining', statement: 'the port 3333 session is cryptocurrency mining', status: 'inconclusive', techniques_cited: [], resolution_reason: 'hunt ended (budget_terminated) with the hypothesis unresolved', provenance: 'operator' },
        { hypothesis_id: 'h-frag', statement: 'at a regular interval, and at least one other host', status: 'inconclusive', techniques_cited: [], resolution_reason: 'hunt ended (budget_terminated) with the hypothesis unresolved', provenance: 'operator' },
      ],
      evidence: [
        { evidence_id: 'ev-1', iteration: 1, source_system: 'net_flow', summary: '3,885 connections at a 30s interval', bears_on: [{ hypothesis_id: 'h-c2', relation: 'supports' }, { hypothesis_id: 'h-frag', relation: 'neither' }] },
        { evidence_id: 'ev-2', iteration: 2, source_system: 'http', summary: 'PowerShell retrieved a 5.5MB logos.png over 3333', bears_on: [{ hypothesis_id: 'h-c2', relation: 'supports' }, { hypothesis_id: 'h-mining', relation: 'weakens' }, { hypothesis_id: 'h-frag', relation: 'neither' }] },
        { evidence_id: 'ev-3', iteration: 3, source_system: 'endpoint', summary: 'sysmon shows powershell.exe opened the socket', bears_on: [{ hypothesis_id: 'h-c2', relation: 'supports' }, { hypothesis_id: 'h-mining', relation: 'weakens' }, { hypothesis_id: 'h-frag', relation: 'neither' }] },
      ],
    }

    it('counts how the rulings landed on each belief, unresolved or not', () => {
      renderPanel({ hunt: hunt(board) })
      tabTo(/Hypotheses/)

      const rows = screen.getAllByRole('row')
      const c2 = rows.find((row) => row.textContent?.includes('command-and-control'))!
      const mining = rows.find((row) => row.textContent?.includes('cryptocurrency mining'))!
      expect(c2.textContent).toContain('3 for · 0 against')
      expect(mining.textContent).toContain('0 for · 2 against')
    })

    // A belief every record was weighed against and set aside is not the same as one
    // nobody has ruled on: the first is a hunt that looked.
    it('tells a belief nothing bore on from one nothing has ruled on', () => {
      renderPanel({ hunt: hunt(board) })
      tabTo(/Hypotheses/)

      const rows = screen.getAllByRole('row')
      expect(rows.find((row) => row.textContent?.includes('at a regular interval'))!.textContent).toContain('not engaged')
    })

    it('says nothing is ruled yet before any evidence bears on a belief', () => {
      renderPanel({ hunt: hunt({ ...board, evidence: [], evidence_count: 0 }) })
      tabTo(/Hypotheses/)

      expect(screen.getAllByText('nothing ruled yet').length).toBe(3)
    })

    // One run-level fact, said once. Printed per row it is the run bar's news again.
    it('hoists the reason every belief shares out of the rows', () => {
      renderPanel({ hunt: hunt(board) })
      tabTo(/Hypotheses/)

      expect(screen.getAllByText(/hunt ended \(budget_terminated\)/)).toHaveLength(1)
    })

    // Counted over what the projection carries, which is capped -- so a long run must
    // not read as though the tally were the whole record.
    it('says so when the rulings are counted over a truncated record', () => {
      renderPanel({ hunt: hunt({ ...board, evidence_count: 63 }) })
      tabTo(/Hypotheses/)

      expect(screen.getByText(/Rulings counted over the 3 most recent/)).toBeInTheDocument()
    })
  })

  it('marks the belief the operator put up themselves', () => {
    renderPanel({
      hunt: hunt({
        hypotheses: [
          { hypothesis_id: 'h-1', statement: 'the definition asked this', status: 'active', provenance: 'hunt_spec' },
          { hypothesis_id: 'h-2', statement: 'I asked this', status: 'active', provenance: 'operator' },
        ],
      }),
    })

    const mine = screen.getByText('I asked this').closest('tr')!
    expect(within(mine).getByText('yours')).toBeInTheDocument()
    const theirs = screen.getByText('the definition asked this').closest('tr')!
    expect(within(theirs).queryByText('yours')).toBeNull()
  })

  it('shows the corroboration a verdict rested on', () => {
    renderPanel({
      hunt: hunt({
        report: {
          gaps: [],
          checkpoints: [],
          hypotheses: [
            {
              hypothesis_id: 'h-3431',
              evidence_strength: {
                corroborating_sources: 4,
                contradicting_records: 0,
                open_gaps: 0,
                attacker_influenceable_only: false,
                survived_disconfirmation: true,
              },
            },
          ],
        },
      }),
    })

    expect(screen.getByText(/4 corroborating source system\(s\)/)).toBeInTheDocument()
    expect(screen.getByText(/survived disconfirmation/)).toBeInTheDocument()
  })
})

// The payoff for declaring an unbound capability: an operator sees the hunt ran
// without a SIEM, rather than reading "nothing was proven" and drawing a
// conclusion about the estate from a fact about the deployment.
describe('what the hunt could not see', () => {
  const blind = {
    gaps: [
      {
        evidence_id: 'ev-1',
        iteration: 0,
        summary: 'no tool in this deployment answers telemetry_search',
        hypothesis_id: null,
      },
    ],
    checkpoints: [],
    hypotheses: [],
  }

  it('lists an unbound capability as a visibility gap', () => {
    renderPanel({ hunt: hunt({ report: blind }) })
    tabTo(/^Gaps/)

    expect(screen.getByText('Visibility gaps (1)')).toBeInTheDocument()
    expect(screen.getByText(/answers telemetry_search/)).toBeInTheDocument()
  })

  it('says a gap that names no hypothesis is unattributed rather than blank', () => {
    renderPanel({ hunt: hunt({ report: blind }) })
    tabTo(/^Gaps/)

    expect(screen.getByText('unattributed')).toBeInTheDocument()
  })

  // Not offered rather than offered empty: a tab that opens onto nothing reads as
  // a section that failed to load.
  it('offers no gap view at all when every query came back', () => {
    renderPanel({ hunt: hunt({ report: { gaps: [], checkpoints: [], hypotheses: [] } }) })

    expect(screen.queryByRole('tab', { name: /^Gaps/ })).toBeNull()
    expect(screen.queryByText(/Visibility gaps/)).toBeNull()
  })
})

describe('escalations and supervision', () => {
  it('links the case an escalation opened', () => {
    renderPanel({
      hunt: hunt({
        handoffs: [
          { case_id: 'case-25aac39c', hypothesis_id: 'h-3431', iteration: 4, rationale: 'isolate FYODOR-L first' },
        ],
      }),
    })
    tabTo(/^Escalations/)

    expect(screen.getByText('Escalated to incident response (1)')).toBeInTheDocument()
    expect(screen.getByText('case-25aac39c')).toBeInTheDocument()
    expect(screen.getByText('isolate FYODOR-L first')).toBeInTheDocument()
  })

  it('shows where a human was asked and what they answered', () => {
    renderPanel({
      hunt: hunt({
        report: {
          gaps: [],
          hypotheses: [],
          checkpoints: [
            {
              checkpoint_id: 'cp-1',
              class: 'verdict_review',
              raised_iteration: 3,
              question: 'Mark h-3431 proven?',
              resolution: { answer: 'approve', actor: 'matthewmorris', text: 'approved' },
            },
          ],
        },
      }),
    })
    tabTo(/^Escalations/)

    expect(screen.getByText('Mark h-3431 proven?')).toBeInTheDocument()
    expect(screen.getByText(/approve by matthewmorris/)).toBeInTheDocument()
  })

  it('says plainly when a checkpoint is still waiting on someone', () => {
    renderPanel({
      hunt: hunt({
        report: {
          gaps: [],
          hypotheses: [],
          checkpoints: [
            { checkpoint_id: 'cp-1', class: 'hypothesis_approval', question: 'Start this hunt?', resolution: null },
          ],
        },
      }),
    })
    tabTo(/^Escalations/)

    expect(screen.getByText('still pending')).toBeInTheDocument()
  })
})

// result_summary only lands at terminal. A hunt that finished but whose row was
// not yet finalised would otherwise show its verdicts and no report.
describe('the report', () => {
  it('renders the hunt report as soon as the projection carries one', () => {
    renderPanel({ hunt: hunt({ report_markdown: '## Verdicts\n\nNothing was proven.' }) })

    expect(screen.getByRole('tab', { name: 'Report', selected: true })).toBeInTheDocument()
    expect(screen.getByText('Nothing was proven.')).toBeInTheDocument()
  })

  it('prefers the hunt report over the run row summary', () => {
    renderPanel({
      result_summary: 'stale row copy',
      hunt: hunt({ report_markdown: 'the ledger says this' }),
    })

    expect(screen.getByText('the ledger says this')).toBeInTheDocument()
    expect(screen.queryByText('stale row copy')).toBeNull()
  })

  // Over half the rendered report was a flat re-listing of the evidence the tab next
  // door shows as a table, under metadata the run bar shows better. The account is
  // the part written nowhere else, so it is what the tab shows.
  it('leads with the account rather than the metadata and the findings dump', () => {
    renderPanel({
      hunt: hunt({
        report_markdown: '# Hunt report\n\n- **Cost:** $4.17\n\n## What the hunt found (34)\n\n- **iteration 5** (dns) — a record nobody needs twice',
        narrative: {
          summary: 'Two unrelated things happened, not one.',
          what_happened: '### Incident 1\n\nA host beaconed to 45.77.53.176.',
          next_steps: ['Isolate FYODOR-L'],
          model_id: 'vertex/gemini-3.5-flash',
          written_at: '2026-08-20T19:00:00.000Z',
        },
      }),
    })

    expect(screen.getByText('Two unrelated things happened, not one.')).toBeInTheDocument()
    expect(screen.getByText(/A host beaconed to 45.77.53.176/)).toBeInTheDocument()
    // The duplicated half is gone, and so is the metadata the run bar already shows.
    expect(screen.queryByText(/a record nobody needs twice/)).toBeNull()
    expect(screen.queryByText(/What the hunt found/)).toBeNull()
  })

  it('points at the tab that does show the records, with the count', () => {
    renderPanel({ hunt: hunt({ report_markdown: 'x', narrative: { summary: 's', what_happened: 'w', next_steps: [], model_id: 'm', written_at: 'w' } }) })

    expect(screen.getByText(/34 record\(s\) gathered/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Evidence/ }))
    expect(screen.getByRole('tab', { name: /Evidence/, selected: true })).toBeInTheDocument()
  })

  // The markdown is what goes out on the terminal and onto a case. Not rendering it
  // inline is not the same as taking it away.
  it('keeps the whole report reachable', () => {
    renderPanel({ hunt: hunt({ report_markdown: 'the whole thing', narrative: { summary: 's', what_happened: 'w', next_steps: [], model_id: 'm', written_at: 'w' } }) })

    expect(screen.getByRole('button', { name: 'copy full report' })).toBeInTheDocument()
  })

  // A run from before there was a narrator, or one whose narrator could not answer.
  it('falls back to the whole report when nothing wrote an account', () => {
    renderPanel({ hunt: hunt({ report_markdown: '## Verdicts\n\nNothing was proven.' }) })

    expect(screen.getByText('Nothing was proven.')).toBeInTheDocument()
  })

  it('gives what to do now its own tab, counted in the label', () => {
    renderPanel({
      hunt: hunt({
        report_markdown: 'x',
        narrative: { summary: 's', what_happened: 'w', next_steps: ['Isolate FYODOR-L', 'Block egress to 45.77.53.176'], model_id: 'm', written_at: 'w' },
      }),
    })

    // The count is on the label, so the tab says how much is outstanding without
    // being opened — which is what makes a tab safe here rather than a banner.
    const tab = screen.getByRole('tab', { name: /Next steps/ })
    expect(tab).toHaveTextContent('2')

    fireEvent.click(tab)
    expect(screen.getByText('What to do now')).toBeInTheDocument()
    expect(screen.getByText('Isolate FYODOR-L')).toBeInTheDocument()
    expect(screen.getByText('Block egress to 45.77.53.176')).toBeInTheDocument()
  })

  it('offers no such tab when the account named no actions', () => {
    renderPanel({ hunt: hunt({ report_markdown: 'x', narrative: { summary: 's', what_happened: 'w', next_steps: [], model_id: 'm', written_at: 'w' } }) })

    expect(screen.queryByRole('tab', { name: /Next steps/ })).toBeNull()
    expect(screen.queryByText('What to do now')).toBeNull()
  })

  // The run bar carries the outcome, the iterations and the cost; "Why it ended" has
  // a line of its own. The report's own header repeated all of it, so a failed run
  // printed its reason three times on one screen.
  it('drops the report header the run bar already shows', () => {
    renderPanel({
      hunt: hunt({
        report_markdown: '# Hunt report\n\n- **Outcome:** failed\n- **Cost:** $0.00 of $100.00\n\n## Verdicts\n\nNothing was proven.',
      }),
    })

    expect(screen.getByText('Nothing was proven.')).toBeInTheDocument()
    expect(screen.queryByText(/\$0.00 of \$100.00/)).toBeNull()
  })

  it('does not print the error a second time when it is the reason', () => {
    const reason = 'the Hunt Lead emitted nothing valid in 3 attempts'
    renderPanel({ status: 'failed', error: reason, hunt: hunt({ status: 'terminal', reason, report_markdown: 'x' }) })

    expect(screen.getAllByText(new RegExp(reason))).toHaveLength(1)
    expect(screen.queryByText('Error')).toBeNull()
  })

  it('still shows an error that says something the reason does not', () => {
    renderPanel({ status: 'failed', error: 'the worker died holding the lease', hunt: hunt({ status: 'terminal', reason: 'ran out of turns', report_markdown: 'x' }) })

    expect(screen.getByText('Error')).toBeInTheDocument()
    expect(screen.getByText(/the worker died holding the lease/)).toBeInTheDocument()
  })

  it('numbers the beliefs in board order and defines them on the board', () => {
    renderPanel({
      hunt: hunt({
        hypotheses: [
          { hypothesis_id: 'h-aaa', statement: 'first belief', status: 'active', techniques_cited: [], provenance: 'operator', resolution_reason: null, attack_technique: null },
          { hypothesis_id: 'h-bbb', statement: 'second belief', status: 'inconclusive', techniques_cited: [], provenance: 'base_rate', resolution_reason: null, attack_technique: null },
        ],
      }),
    })

    fireEvent.click(screen.getByRole('tab', { name: /Hypotheses/ }))
    expect(screen.getByTitle('h-aaa')).toHaveTextContent('H1')
    expect(screen.getByTitle('h-bbb')).toHaveTextContent('H2')
  })

  // A reference the board does not hold is worth seeing, not eliding.
  it('falls back to the id for a belief this projection never carried', () => {
    renderPanel({
      hunt: hunt({
        evidence: [{
          evidence_id: 'ev-1', iteration: 1, source_system: 'dns', summary: 'a record', why_notable: '',
          salience: 'anomalous', attack_technique: null, attacker_influenceable: false, instruction_like: false,
          provenance: 'worker', is_gap: false, gap_detail: null, captured_at: '2026-08-20T00:00:00Z',
          bears_on: [{ hypothesis_id: 'h-not-on-the-board', relation: 'supports' }],
        }],
      }),
    })

    fireEvent.click(screen.getByRole('tab', { name: /Evidence/ }))
    expect(screen.getByTitle('h-not-on-the-board')).toHaveTextContent('h-not-on-the-board')
  })

  // The lead rules every observation against every belief, so most rulings say
  // "does not bear on". On one run 46 of 63 links were `neither`: three quarters of
  // the column, saying nothing a reader can act on.
  it('shows what a record bears on, not what it does not', () => {
    renderPanel({
      hunt: hunt({
        evidence: [{
            evidence_id: 'ev-1', iteration: 1, source_system: 'dns', summary: 'a record', why_notable: '',
            salience: 'anomalous', attack_technique: null, attacker_influenceable: false, instruction_like: false,
            provenance: 'worker', is_gap: false, gap_detail: null, captured_at: '2026-08-20T00:00:00Z',
            bears_on: [{ hypothesis_id: 'h-3431', relation: 'supports' }, { hypothesis_id: 'h-other', relation: 'neither' }],
          }],
      }),
    })

    fireEvent.click(screen.getByRole('tab', { name: /Evidence/ }))
    expect(screen.getByText(/supports/)).toBeInTheDocument()
    expect(screen.queryByText(/neither/)).toBeNull()
  })

  it('tells a record it weighed and set aside from one nobody has ruled on', () => {
    renderPanel({
      hunt: hunt({
        evidence: [{
            evidence_id: 'ev-2', iteration: 1, source_system: 'dns', summary: 'a record', why_notable: '',
            salience: 'anomalous', attack_technique: null, attacker_influenceable: false, instruction_like: false,
            provenance: 'worker', is_gap: false, gap_detail: null, captured_at: '2026-08-20T00:00:00Z',
            bears_on: [{ hypothesis_id: 'h-3431', relation: 'neither' }],
          }],
      }),
    })

    fireEvent.click(screen.getByRole('tab', { name: /Evidence/ }))
    expect(screen.getByText('bears on none')).toBeInTheDocument()
  })

  // A negative result is evidence, and knowing the hunt looked matters. Folded
  // rather than dropped.
  it('folds routine records behind a count and opens them on request', () => {
    renderPanel({
      hunt: hunt({
        evidence: [{
            evidence_id: 'ev-3', iteration: 1, source_system: 'dns', summary: 'what was anomalous', why_notable: '',
            salience: 'anomalous', attack_technique: null, attacker_influenceable: false, instruction_like: false,
            provenance: 'worker', is_gap: false, gap_detail: null, captured_at: '2026-08-20T00:00:00Z',
            bears_on: [],
          }, {
            evidence_id: 'ev-4', iteration: 1, source_system: 'dns', summary: 'nothing matched', why_notable: '',
            salience: 'routine', attack_technique: null, attacker_influenceable: false, instruction_like: false,
            provenance: 'worker', is_gap: false, gap_detail: null, captured_at: '2026-08-20T00:00:00Z',
            bears_on: [],
          }],
      }),
    })

    fireEvent.click(screen.getByRole('tab', { name: /Evidence/ }))
    expect(screen.getByText('what was anomalous')).toBeInTheDocument()
    expect(screen.queryByText('nothing matched')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '1 routine hidden' }))
    expect(screen.getByText('nothing matched')).toBeInTheDocument()
  })

  // The account separates the incidents; rendering it as one markdown run throws that
  // structure away on the way to the screen.
  it('gives each incident the account separated its own card', () => {
    renderPanel({
      hunt: hunt({
        report_markdown: 'x',
        narrative: {
          summary: 's',
          what_happened: '### Memcached exploitation\n\nInbound UDP to 172.16.0.178.\n\n### Inbound SSH\n\nFrom 118.163.24.179.',
          next_steps: [], model_id: 'm', written_at: 'w',
        },
      }),
    })

    expect(screen.getByText('Memcached exploitation')).toBeInTheDocument()
    expect(screen.getByText('Inbound SSH')).toBeInTheDocument()
    expect(screen.getByText(/Inbound UDP to 172.16.0.178/)).toBeInTheDocument()
    // Numbered by the console: the writer's own headings do not number themselves,
    // and "the second one" is how a person refers to a list out loud.
    expect(screen.getByText('01')).toBeInTheDocument()
    expect(screen.getByText('02')).toBeInTheDocument()
  })

  it('leaves an account that separated nothing as one piece', () => {
    renderPanel({
      hunt: hunt({
        report_markdown: 'x',
        narrative: { summary: 's', what_happened: 'One thing happened, told whole.', next_steps: [], model_id: 'm', written_at: 'w' },
      }),
    })

    expect(screen.getByText('One thing happened, told whole.')).toBeInTheDocument()
  })

  // Nine chips saying "inconclusive" nine times is the board restated, not a verdict.
  it('groups the standings by count rather than listing every belief', () => {
    renderPanel({
      hunt: hunt({
        report_markdown: 'x',
        narrative: { summary: 's', what_happened: 'w', next_steps: [], model_id: 'm', written_at: 'w' },
        hypotheses: [
          { hypothesis_id: 'h-a', statement: 'one', status: 'inconclusive', techniques_cited: [], provenance: 'operator', resolution_reason: null, attack_technique: null },
          { hypothesis_id: 'h-b', statement: 'two', status: 'inconclusive', techniques_cited: [], provenance: 'operator', resolution_reason: null, attack_technique: null },
          { hypothesis_id: 'h-c', statement: 'the benign account', status: 'active', techniques_cited: [], provenance: 'base_rate', resolution_reason: null, attack_technique: null },
        ],
      }),
    })

    // Two of the operator's, counted once. The base rate is machinery, not a belief
    // they set out to test, so it is not in the count.
    expect(screen.getByText('2 beliefs tested ▸')).toBeInTheDocument()
    expect(screen.queryByText('3 beliefs tested ▸')).toBeNull()
  })

  it('marks the belief the loop seeded, and puts it after the ones asked for', () => {
    renderPanel({
      hunt: hunt({
        hypotheses: [
          { hypothesis_id: 'h-c', statement: 'the benign account', status: 'active', techniques_cited: [], provenance: 'base_rate', resolution_reason: null, attack_technique: null },
          { hypothesis_id: 'h-a', statement: 'what I asked', status: 'active', techniques_cited: [], provenance: 'operator', resolution_reason: null, attack_technique: null },
        ],
      }),
    })

    fireEvent.click(screen.getByRole('tab', { name: /Hypotheses/ }))
    expect(screen.getByText('the claim to beat')).toBeInTheDocument()
    const rows = screen.getAllByRole('row').map((r) => r.textContent ?? '')
    expect(rows.findIndex((t) => t.includes('what I asked'))).toBeLessThan(rows.findIndex((t) => t.includes('the benign account')))
  })

  it('still shows a plain result summary for a run that is not a hunt', () => {
    renderPanel({ result_summary: 'phases all ran' })

    expect(screen.getByText('Result summary')).toBeInTheDocument()
    expect(screen.getByText('phases all ran')).toBeInTheDocument()
  })
})

// A fan-out dispatches one query_intent to every worker, so four failures printed
// the same 300-char intent four times with only the trailing reason differing.
describe('gaps read as questions, not as workers', () => {
  const same = (id: string, summary = 'worker failed: calls_exhausted') => ({
    evidence_id: id,
    iteration: 2,
    summary,
    query_intent: 'Determine reputation and ASN for 45.77.53.176',
    hypothesis_id: 'h-3431',
  })

  it('shows the question once and counts the workers', () => {
    renderPanel({
      hunt: hunt({
        report: {
          gaps: [same('ev-1'), same('ev-2'), same('ev-3', 'worker failed: timeout')],
          checkpoints: [],
          hypotheses: [],
        },
      }),
    })
    tabTo(/^Gaps/)

    expect(screen.getAllByText(/Determine reputation and ASN/)).toHaveLength(1)
    expect(screen.getByText('3 workers, same question.')).toBeInTheDocument()
  })

  it('keeps every distinct reason', () => {
    renderPanel({
      hunt: hunt({
        report: {
          gaps: [same('ev-1'), same('ev-2', 'worker failed: timeout')],
          checkpoints: [],
          hypotheses: [],
        },
      }),
    })
    tabTo(/^Gaps/)

    expect(screen.getByText('worker failed: calls_exhausted')).toBeInTheDocument()
    expect(screen.getByText('worker failed: timeout')).toBeInTheDocument()
  })

  // The header still counts records, not questions: three failed workers are
  // three blind spots even when they were blind to the same thing.
  it('still counts the records in the heading', () => {
    renderPanel({
      hunt: hunt({ report: { gaps: [same('ev-1'), same('ev-2')], checkpoints: [], hypotheses: [] } }),
    })
    tabTo(/^Gaps/)

    expect(screen.getByText('Visibility gaps (2)')).toBeInTheDocument()
  })
})

// approve and reject have always been valid directives and the projection has
// always carried the open checkpoint. Nothing rendered it, so a parked hunt could
// be watched and not answered, and the only way out of one was to abort it.
describe('answering a checkpoint the hunt is parked on', () => {
  const parked = (over = {}) => ({
    run_id: 'run-1',
    checkpoint_id: 'cp-5d413252',
    checkpoint_class: 'budget_anomaly',
    question: 'Two consecutive worker dispatches have failed with 504 timeout errors.',
    ...over,
  })

  const withCheckpoint = (over = {}) =>
    renderPanel({ hunt: hunt({ run_id: 'run-1', open_checkpoint: parked(over) }) })

  it('shows what the run is waiting on rather than only that it waits', () => {
    withCheckpoint()

    expect(screen.getByText(/Two consecutive worker dispatches have failed/)).toBeInTheDocument()
    expect(screen.getByText(/Waiting on you · budget_anomaly/)).toBeInTheDocument()
  })

  it('sends the answer against the checkpoint it was raised for', async () => {
    const { workflowApi } = await import('../../../services/api')
    withCheckpoint()

    fireEvent.click(screen.getByRole('button', { name: 'approve' }))

    expect(workflowApi.steer).toHaveBeenCalledWith('run-1', 'approve', '', {
      checkpoint_id: 'cp-5d413252',
    })
  })

  it('carries the reason typed beside the buttons', async () => {
    const { workflowApi } = await import('../../../services/api')
    withCheckpoint()

    fireEvent.change(screen.getByPlaceholderText(/Why —/), { target: { value: 'timeout is fixed' } })
    fireEvent.click(screen.getByRole('button', { name: 'reject' }))

    expect(workflowApi.steer).toHaveBeenCalledWith('run-1', 'reject', 'timeout is fixed', {
      checkpoint_id: 'cp-5d413252',
    })
  })

  // The approval checkpoint carries them, and they are the reason an operator
  // would reject rather than approve.
  it('names the capabilities the run has no tool for', () => {
    withCheckpoint({ context: { unbound_capabilities: ['telemetry_search'] } })

    expect(screen.getByText(/No tool here answers telemetry_search/)).toBeInTheDocument()
  })

  // The projection keeps reporting the checkpoint until the run journals a
  // resolution, so the panel stayed as a wall of text under "Waiting on you" with
  // both buttons live — after the operator had already answered, and while it was
  // waiting on the run rather than on them.
  it('collapses to what was sent once it is answered', async () => {
    withCheckpoint()

    fireEvent.click(screen.getByRole('button', { name: 'approve' }))

    expect(await screen.findByText(/picks it up at its next turn/)).toBeInTheDocument()
    expect(screen.queryByText(/Waiting on you/)).toBeNull()
    expect(screen.queryByText(/Two consecutive worker dispatches/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'approve' })).toBeNull()
  })

  // Otherwise a second question arrives already wearing the first one's answer,
  // and the run waits on somebody who has been told it is handled.
  it('asks again when the run raises a different question', async () => {
    const { rerender } = withCheckpoint()
    fireEvent.click(screen.getByRole('button', { name: 'approve' }))
    await screen.findByText(/picks it up at its next turn/)

    rerender(
      <RunDetail
        d={detail({ hunt: hunt({ run_id: 'run-1', open_checkpoint: parked({ checkpoint_id: 'cp-later', question: 'Mark h-3431 proven?' }) }) })}
        onSteered={() => {}}
      />,
    )

    expect(screen.getByText('Mark h-3431 proven?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'approve' })).toBeInTheDocument()
  })

  it('shows nothing when the run is waiting on no one', () => {
    renderPanel({ hunt: hunt() })

    expect(screen.queryByText(/Waiting on you/)).toBeNull()
  })
})

// Stop ended the run and its spend on one click, styled as a peer of the notes
// the lead reads at its next turn. It now sits with the run's own status and asks.
describe('ending a run', () => {
  const live = () => renderPanel({ status: 'running', hunt: hunt({ status: 'running' }) })

  it('asks before it fires, and does not fire on the ask', async () => {
    const { workflowApi } = await import('../../../services/api')
    live()

    fireEvent.click(screen.getByRole('button', { name: /Stop/ }))

    expect(screen.getByText(/It cannot be resumed/)).toBeInTheDocument()
    expect(workflowApi.cancelRun).not.toHaveBeenCalled()
  })

  it('cancels the run once confirmed', async () => {
    const { workflowApi } = await import('../../../services/api')
    live()

    fireEvent.click(screen.getByRole('button', { name: /Stop/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(workflowApi.cancelRun).toHaveBeenCalledWith('run-1', 'stopped from the console')
  })

  it('offers nothing to stop on a run that already ended', () => {
    renderPanel({ hunt: hunt() })

    expect(screen.queryByRole('button', { name: /Stop/ })).toBeNull()
  })
})

// The panel reported "N pieces of evidence gathered" and nothing else, so for the
// whole of a run -- which is when somebody is watching -- an operator could see
// that it had found things and never what any of them were.
describe('what the hunt has actually gathered', () => {
  const record = (over = {}) => ({
    evidence_id: 'ev-1',
    iteration: 2,
    source_system: 'splunk',
    summary: 'HOST-42 reached 45.77.53.176 every 30s for four hours',
    why_notable: 'low jitter across a long window',
    salience: 'anomalous',
    bears_on: [{ hypothesis_id: 'h-3431', relation: 'supports' }],
    ...over,
  })

  const withEvidence = (records: object[], count = records.length) =>
    renderPanel({ hunt: hunt({ evidence: records, evidence_count: count }) })

  it('shows what a record says and which belief it bears on', () => {
    withEvidence([record()])
    tabTo(/^Evidence/)

    expect(screen.getByText(/reached 45.77.53.176 every 30s/)).toBeInTheDocument()
    expect(screen.getByText('low jitter across a long window')).toBeInTheDocument()
    // Referred to by its place on the board, not by its hash. The id is one hover
    // away, because it is what the ledger and the report use.
    expect(screen.getByText(/supports/)).toBeInTheDocument()
    expect(screen.getByTitle('h-3431')).toHaveTextContent('H1')
  })

  // A record nobody linked is the case most worth seeing: it was gathered and then
  // nothing was concluded from it.
  it('says so when a record bears on nothing', () => {
    withEvidence([record({ bears_on: [] })])
    tabTo(/^Evidence/)

    expect(screen.getByText('nothing yet')).toBeInTheDocument()
  })

  // What the verdict gate reads, so an operator can see why support did not carry
  // rather than only that it did not.
  it('marks a record a verdict cannot rest on alone', () => {
    withEvidence([record({ sensor_attested: false, instruction_like: true })])
    tabTo(/^Evidence/)

    expect(screen.getByText('nothing sensor-attested')).toBeInTheDocument()
    expect(screen.getByText('reads as instruction')).toBeInTheDocument()
  })

  // "attacker-influenceable" was on every record of a real run: in a hunt the
  // adversary's behaviour is the signal, so attacker-caused is universal and said
  // nothing. What carries a verdict is whether the telemetry attested anything.
  it('says a record is attested even where the adversary chose some of it', () => {
    withEvidence([
      record({
        sensor_attested: true,
        rests_on: [
          { field: 'conn_count', authored: 'sensor' },
          { field: 'dest_ip', authored: 'adversary' },
        ],
      }),
    ])
    tabTo(/^Evidence/)

    expect(screen.queryByText('nothing sensor-attested')).toBeNull()
    // And still names what the adversary did choose, which is the critic's best lever.
    expect(screen.getByText('1 attacker-authored field')).toBeInTheDocument()
  })

  // A ledger written before the split carries the boolean and no basis at all.
  it('falls back to the old flag on a run written before the split', () => {
    withEvidence([record({ attacker_influenceable: true })])
    tabTo(/^Evidence/)

    expect(screen.getByText('nothing sensor-attested')).toBeInTheDocument()
  })

  it('separates a blind spot from a finding', () => {
    withEvidence([record({ is_gap: true, summary: 'worker failed: 504 timeout' })])
    tabTo(/^Evidence/)

    expect(screen.getByText(/a blind spot, not a finding/)).toBeInTheDocument()
  })

  // Capped by the projection, and the count is the untruncated total, so the panel
  // has to say it is showing a subset rather than implying that is all there was.
  it('says when it is showing fewer records than the run gathered', () => {
    withEvidence([record()], 120)
    tabTo(/^Evidence/)

    expect(screen.getByText(/showing 1 of 120/)).toBeInTheDocument()
  })

  it('offers no evidence view for a run that gathered none', () => {
    renderPanel({ hunt: hunt({ evidence: [], evidence_count: 0 }) })

    expect(screen.queryByRole('tab', { name: /^Evidence/ })).toBeNull()
  })

  // report.gaps only exists once the hunt writes a report, so mid-run the operator
  // could not see what it had failed to look at -- the half that costs money to
  // rediscover.
  it('shows a gap while the run is still going, before any report exists', () => {
    withEvidence([record({ is_gap: true, summary: 'stream:dns returned nothing in the window queried' })])
    tabTo(/^Gaps/)

    expect(screen.getByText('Visibility gaps (1)')).toBeInTheDocument()
    expect(screen.getByText(/stream:dns returned nothing/)).toBeInTheDocument()
  })
})

// A run that stopped at the ceiling its operator set did what it was told. The
// bridge wrote its reason into the error column and the panel rendered that under
// a red "Error" heading, so "an operator accepted the stop at the budget
// checkpoint" was shown as a fault.
/* A parked hunt has not ended, so the run row still reads "running" and "Why it ended"
   stays hidden — which left the console saying nothing at all about a hunt sitting at
   its own ceiling waiting to be told what to do. Observed live: iteration 3 of 3, no
   checkpoint, and nothing telling the operator to reach for Steer. */
describe('a hunt waiting to be told what to do next', () => {
  it('says it is waiting, and why, while the run still reads as running', () => {
    renderPanel({
      status: 'running',
      hunt: hunt({
        status: 'parked',
        outcome: null,
        open_checkpoint: null,
        reason: 'ran out of turns: iteration 3 of 3, having spent $0.11 of $14.00',
      }),
    })

    expect(screen.getByText(/Stopped and waiting: ran out of turns: iteration 3 of 3/)).toBeInTheDocument()
    // And it names the way out, which is a control on this panel rather than a syntax
    // the operator has to get right.
    expect(screen.getByText(/below to let it carry on from here/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '+3 turns' })).toBeInTheDocument()
  })

  // `extend` with an empty note parsed to no grant, journaled a note saying so, and left
  // the hunt parked at the ceiling it was asking to be let past. One real run spent 7 of
  // 7 iterations with $14.50 unspent that way, then concluded with nothing proven.
  it('sends a typed grant rather than prose the run has to parse', async () => {
    const { workflowApi } = await import('../../../services/api')
    renderPanel({
      status: 'running',
      hunt: hunt({ status: 'parked', outcome: null, open_checkpoint: null, reason: 'ran out of turns' }),
    })

    fireEvent.click(screen.getByRole('button', { name: '+3 turns' }))

    expect(workflowApi.steer).toHaveBeenCalledWith('run-1', 'extend', '', {
      grant: { iterations: 3, cost_usd: 0, wall_ms: 0 },
    })
  })

  // Turns are not always the arm that bound: the wall clock stops a hunt with money and
  // iterations still on the board, and one press has to be able to buy the arm that ran out.
  // A press disables the row until it settles, so each is its own render.
  it.each([
    ['+$5', { iterations: 0, cost_usd: 5, wall_ms: 0 }],
    ['+30 min', { iterations: 0, cost_usd: 0, wall_ms: 1_800_000 }],
  ])('grants what %s says', async (label, grant) => {
    const { workflowApi } = await import('../../../services/api')
    renderPanel({ status: 'running', hunt: hunt({ status: 'parked', outcome: null, open_checkpoint: null }) })

    fireEvent.click(screen.getByRole('button', { name: label }))

    expect(workflowApi.steer).toHaveBeenCalledWith('run-1', 'extend', '', { grant })
  })

  it('stays quiet while the hunt is actually working', () => {
    renderPanel({ status: 'running', hunt: hunt({ status: 'active', outcome: null, open_checkpoint: null }) })

    expect(screen.queryByText(/Stopped and waiting/)).toBeNull()
  })

  // A real checkpoint has its own panel with an approve and a reject on it; saying it
  // twice, in two voices, would be worse than saying it once.
  it('defers to the checkpoint panel when there is a checkpoint to answer', () => {
    renderPanel({
      status: 'running',
      hunt: hunt({
        status: 'parked',
        outcome: null,
        reason: 'awaiting an operator',
        open_checkpoint: { checkpoint_id: 'cp-1', checkpoint_class: 'scope_extension', question: 'widen to 10.0.0.0/8?', raised_at: '2026-08-20T00:00:00Z', context: {} },
      }),
    })

    // The checkpoint panel already says its own piece, with an approve and a reject on
    // it; saying it twice in two voices would be worse than saying it once.
    expect(screen.queryByText(/Stopped and waiting/)).toBeNull()
  })
})

describe('why a finished run ended', () => {
  it('states the reason beside the outcome rather than as an error', () => {
    renderPanel({
      status: 'completed',
      hunt: hunt({ outcome: 'budget_terminated', reason: 'ran out of turns: iteration 3 of 3, having spent $0.11 of $14.00' }),
    })

    expect(screen.getByText(/Why it ended: ran out of turns: iteration 3 of 3/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Error' })).toBeNull()
  })

  it('still shows a real error as one', () => {
    renderPanel({ status: 'failed', error: 'its spec cannot be built' })

    expect(screen.getByRole('heading', { name: 'Error' })).toBeInTheDocument()
  })
})

/* The standings say what the hunt believes and the evidence says what came back;
   neither says which move the lead made, so a run in flight showed a turn counter
   advancing over no account of what it decided. */
describe('the moves the lead made', () => {
  const moves = [
    {
      decision_id: 'dec-2',
      iteration: 2,
      action: 'VALIDATE',
      rationale: 'the beaconing interval is worth putting up for a verdict',
      target_hypothesis_id: 'h-3431',
      rejected_attempts: ['VALIDATE must cite the evidence it rests on'],
    },
    {
      decision_id: 'dec-1',
      iteration: 1,
      action: 'INVESTIGATE',
      rationale: 'start on the flow telemetry',
      query_intent: 'what did 10.0.0.5 talk to, and how regularly',
      worker_agent_id: 'network_analyst',
    },
  ]

  it('shows each decision, what it asked and what it targeted', () => {
    renderPanel({ hunt: hunt({ moves }) })
    tabTo(/Moves/)

    expect(screen.getByText('VALIDATE')).toBeInTheDocument()
    expect(screen.getByText(/start on the flow telemetry/)).toBeInTheDocument()
    expect(screen.getByText(/what did 10.0.0.5 talk to/)).toBeInTheDocument()
    expect(screen.getByText('network_analyst')).toBeInTheDocument()
  })

  it('says when a turn had to re-ask, which is the only account of one that stalled', () => {
    renderPanel({ hunt: hunt({ moves }) })
    tabTo(/Moves/)

    expect(screen.getByText(/1 emission\(s\) refused first/)).toBeInTheDocument()
  })

  it('offers no tab at all on a run that recorded none', () => {
    renderPanel({ hunt: hunt() })

    expect(screen.queryByRole('tab', { name: /Moves/ })).toBeNull()
  })
})

/* boost has always been a directive and it names a question_id the console never
   showed, so the frontier was both invisible and unsteerable. */
describe('the frontier', () => {
  const open_questions = [
    { question_id: 'q-1', question: '45.77.53.176', entity_key: 'ip:45.77.53.176', hypothesis_id: 'h-3431', spawned_iteration: 2 },
  ]

  it('lists the leads nobody has taken', () => {
    renderPanel({ hunt: hunt({ open_questions }) })
    tabTo(/Frontier/)

    expect(screen.getByText('ip:45.77.53.176')).toBeInTheDocument()
    expect(screen.getByTitle('h-3431')).toHaveTextContent('H1')
  })

  it('pins one for the next turn on a run still in flight', async () => {
    const { workflowApi } = await import('../../../services/api')
    renderPanel({ status: 'running', hunt: hunt({ status: 'active', open_questions }) })
    tabTo(/Frontier/)
    fireEvent.click(screen.getByRole('button', { name: /take next/ }))

    expect(workflowApi.steer).toHaveBeenCalledWith('run-1', 'boost', '', { question_id: 'q-1' })
  })

  it('offers no control on a run that has ended', () => {
    renderPanel({ hunt: hunt({ open_questions }) })
    tabTo(/Frontier/)

    expect(screen.queryByRole('button', { name: /take next/ })).toBeNull()
  })
})

/* The lead directive grows what a hunt looks at and the controller has always
   handled it; the console offered no way to send one. */
describe('steering a hunt that is still going', () => {
  it('puts an operator lead on the frontier, with the entity it is about', async () => {
    const { workflowApi } = await import('../../../services/api')
    renderPanel({ status: 'running', hunt: hunt({ status: 'active' }) })
    fireEvent.change(screen.getByPlaceholderText(/A note for the run/), { target: { value: 'check the jump box too' } })
    fireEvent.change(screen.getByPlaceholderText(/type:value/), { target: { value: 'ip:10.0.0.5' } })
    fireEvent.click(screen.getByRole('button', { name: /add lead/ }))

    expect(workflowApi.steer).toHaveBeenCalledWith('run-1', 'lead', 'check the jump box too', { entity_key: 'ip:10.0.0.5' })
  })
})


// The two tables a reader spends the most time in, and the two that a real run made
// unreadable: a 300-character raw emission in a table cell, and an ip wrapped one
// character to a line down a tight column.
describe('the moves table', () => {
  const move = (over = {}) => ({
    decision_id: 'd-1',
    iteration: 7,
    action: 'VALIDATE',
    rationale: 'putting the primary hypothesis to a verdict',
    ...over,
  })

  it('says why an emission was refused without reprinting the emission', () => {
    const rejection =
      '/query_intent must be string: { "action": "VALIDATE", "rationale": "With the hunt budget fully depleted on this final turn, we are putting our primary hypothesis h-1f0e78e3 to a verdict. The network flow telemetry confirms" }'
    renderPanel({ hunt: hunt({ moves: [move({ rejected_attempts: [rejection] })] }) })
    tabTo(/Moves/)

    expect(screen.getByText(/1 emission\(s\) refused first — \/query_intent must be string/)).toBeInTheDocument()
    expect(screen.queryByText(/network flow telemetry confirms/)).toBeNull()
  })

  it('says something rather than nothing when the refusal carries no complaint', () => {
    renderPanel({ hunt: hunt({ moves: [move({ rejected_attempts: ['{"action":"NOPE"}'] })] }) })
    tabTo(/Moves/)

    expect(screen.getByText(/did not match the schema/)).toBeInTheDocument()
  })

  // In the tight On column an ip wrapped to one character a line, over eight lines.
  it('carries the entity and the worker beside the rationale, not in the tight column', () => {
    renderPanel({
      hunt: hunt({ moves: [move({ target_entity: 'ip:192.168.70.186', worker_agent_id: 'threat_hunter', target_hypothesis_id: 'h-3431' })] }),
    })
    tabTo(/Moves/)

    const cells = screen.getAllByRole('cell')
    const why = cells.find((cell) => cell.textContent?.includes('putting the primary hypothesis'))!
    expect(why.textContent).toContain('ip:192.168.70.186')
    expect(why.textContent).toContain('threat_hunter')
    // On carries the belief reference and nothing that has to wrap.
    expect(cells[cells.length - 1].textContent).toBe('H1')
  })
})

describe('the evidence table', () => {
  const record = (over = {}) => ({
    evidence_id: 'ev-1',
    iteration: 2,
    source_system: 'endpoint',
    summary: 'sysmon shows powershell.exe opened the socket',
    salience: 'anomalous',
    ...over,
  })

  // "weakens H6 / weakens H7 / weakens H9" stacked three lines down a narrow column
  // and made the row taller than the summary it belongs to.
  it('groups the beliefs a record bears on by relation', () => {
    renderPanel({
      hunt: hunt({
        evidence_count: 1,
        hypotheses: [
          { hypothesis_id: 'h-a', statement: 'C2', status: 'active' },
          { hypothesis_id: 'h-b', statement: 'mining', status: 'active' },
          { hypothesis_id: 'h-c', statement: 'mining pool', status: 'active' },
        ],
        evidence: [
          record({
            bears_on: [
              { hypothesis_id: 'h-a', relation: 'supports' },
              { hypothesis_id: 'h-b', relation: 'weakens' },
              { hypothesis_id: 'h-c', relation: 'weakens' },
            ],
          }),
        ],
      }),
    })
    tabTo(/Evidence/)

    // One line per relation, so three rulings are two lines rather than three.
    expect(screen.getByText(/^supports/).textContent?.replace(/\s+/g, ' ').trim()).toBe('supports H1')
    expect(screen.getByText(/^weakens/).textContent?.replace(/\s+/g, ' ').trim()).toBe('weakens H2 H3')
  })
})
