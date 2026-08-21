/**
 * The approvals queue is where a parked agent run gets answered. Compose steps
 * carry a phase id; hunt and investigate park on a checkpoint class instead, and
 * before this the column simply read "—" for them.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { checkpointChip } from './DecisionsScreen'
import type { ApprovalAction } from './useDecisions'

function action(over: Partial<ApprovalAction>): ApprovalAction {
  return { action_id: 'act-1', workflow_run_id: '9c1c2d3e-0000-4000-8000-000000000634', ...over }
}

describe('what a pending approval says it is waiting on', () => {
  it('names the checkpoint class for a run that parked on one', () => {
    render(checkpointChip(action({ parameters: { checkpoint_class: 'verdict_review', checkpoint_id: 'chk-7' } })))
    expect(screen.getByText('verdict_review')).toBeInTheDocument()
  })

  // The id is what a resolution has to answer, so it is reachable without taking
  // a column of its own.
  it('carries the checkpoint id as the chip title', () => {
    render(checkpointChip(action({ parameters: { checkpoint_class: 'tool_approval', checkpoint_id: 'apr-abc123' } })))
    expect(screen.getByText('tool_approval')).toHaveAttribute('title', 'apr-abc123')
  })

  // A compose step has a phase and no checkpoint class, and must read as it did.
  it('still shows a compose phase id', () => {
    render(checkpointChip(action({ workflow_phase_id: 'phase-triage' })))
    expect(screen.getByText('phase-triage')).toBeInTheDocument()
  })

  it('falls back to a dash when the approval names neither', () => {
    render(checkpointChip(action({})))
    expect(screen.getByText('\u2014')).toBeInTheDocument()
  })
})
