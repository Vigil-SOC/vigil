import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { HoldButton } from './HoldButton'
import { InfoTip } from './InfoTip'
import { LevelBadge } from './LevelBadge'
import { NotMeasured } from './NotMeasured'

describe('LevelBadge', () => {
  it('writes the server level as a word, and a dash when unmeasured', () => {
    const { rerender } = render(<LevelBadge level="fair" />)
    expect(screen.getByText('Fair')).toBeInTheDocument()
    rerender(<LevelBadge level={null} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('pill variant tints by level and draws nothing when unmeasured', () => {
    const { container, rerender } = render(<LevelBadge level="poor" variant="pill" />)
    expect(screen.getByText('Poor')).toHaveClass('level-pill', 'poor')
    rerender(<LevelBadge level={null} variant="pill" />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('InfoTip', () => {
  const tip = <InfoTip label="How it works" source="Events" calculation="Count" limit="Set in Settings" />

  it('opens on click with the three lines, closes on Escape', () => {
    render(tip)
    const button = screen.getByRole('button', { name: 'How it works' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    fireEvent.click(button)
    const pop = screen.getByRole('tooltip')
    expect(pop).toHaveTextContent('Source Events')
    expect(pop).toHaveTextContent('Calculation Count')
    expect(pop).toHaveTextContent('Limit Set in Settings')
    fireEvent.keyDown(button, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('closes on blur and on a click elsewhere', () => {
    render(tip)
    const button = screen.getByRole('button', { name: 'How it works' })
    fireEvent.click(button)
    fireEvent.blur(button)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    fireEvent.click(button)
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })
})

describe('NotMeasured', () => {
  it('shows the placeholder, with an ⓘ only when it has something to say', () => {
    const { rerender } = render(<NotMeasured label="Retention" />)
    expect(screen.getByText('Retention: Not measured yet')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    rerender(<NotMeasured tip="Not recorded." />)
    fireEvent.click(screen.getByRole('button', { name: 'Not recorded.' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('Not recorded.')
  })
})

describe('HoldButton', () => {
  it('confirms after the full hold, cancels on early release, and names the action', () => {
    vi.useFakeTimers()
    const onConfirm = vi.fn()
    render(<HoldButton label="Revoke" disabled={false} onConfirm={onConfirm} />)
    const button = screen.getByRole('button', { name: 'Revoke. Press and hold to confirm; this cannot be undone.' })

    fireEvent.pointerDown(button)
    expect(button).toHaveTextContent('Keep holding…')
    act(() => void vi.advanceTimersByTime(800))
    fireEvent.pointerUp(button)
    act(() => void vi.advanceTimersByTime(1600))
    expect(onConfirm).not.toHaveBeenCalled()
    expect(button).toHaveTextContent('Revoke')

    fireEvent.keyDown(button, { key: ' ' })
    act(() => void vi.advanceTimersByTime(1600))
    expect(onConfirm).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
  })

  it('shows the done state and stops taking holds', () => {
    vi.useFakeTimers()
    const onConfirm = vi.fn()
    render(<HoldButton label="Isolate" done="Isolated · FIN-WS-0231" disabled={false} onConfirm={onConfirm} />)
    const button = screen.getByRole('button', { name: 'Isolated · FIN-WS-0231' })
    expect(button).toBeDisabled()
    fireEvent.pointerDown(button)
    act(() => void vi.advanceTimersByTime(1600))
    expect(onConfirm).not.toHaveBeenCalled()
    vi.useRealTimers()
  })
})
