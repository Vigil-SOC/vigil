import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import DashboardScreen from './DashboardScreen'

vi.mock('./FindingPopup', () => ({
  default: ({ onCaseCreated }: { onCaseCreated?: (caseId: string) => void }) => (
    <button onClick={() => onCaseCreated?.('case/linked')}>Complete linked case</button>
  ),
}))

vi.mock('./useFindings', () => ({
  useFindings: () => ({ rows: [], phase: 'ready', error: null, reload: vi.fn() }),
  useDashboardKpis: () => ({ kpis: null, reload: vi.fn() }),
}))

describe('DashboardScreen linked-case navigation', () => {
  it('opens the created case detail using an encoded query parameter', () => {
    const go = vi.fn()
    render(
      <DashboardScreen
        openChat={vi.fn()}
        go={go}
        goSettings={vi.fn()}
        setViewFull={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Complete linked case' }))
    expect(go).toHaveBeenCalledWith('cases', { search: '?case=case%2Flinked' })
  })
})
