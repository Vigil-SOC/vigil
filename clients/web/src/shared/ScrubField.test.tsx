import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { ScrubField } from './ScrubField'

// jsdom has no PointerEvent, so clientX and button would be dropped
class TestPointerEvent extends MouseEvent {
  pointerId = 1
}
vi.stubGlobal('PointerEvent', TestPointerEvent)

function setup(initial = 12) {
  const onCommit = vi.fn()
  function Host() {
    const [v, setV] = useState(initial)
    return (
      <ScrubField
        name="Budget per case"
        label="Budget"
        prefix="$"
        unit="per case"
        value={v}
        min={1}
        max={60}
        step={1}
        onCommit={(n) => {
          onCommit(n)
          setV(n)
        }}
      />
    )
  }
  render(<Host />)
  return { onCommit, field: () => screen.getByRole('spinbutton', { name: 'Budget per case' }) }
}

describe('ScrubField', () => {
  it('exposes its value and bounds, and steps with the arrows (Shift x10)', () => {
    const { onCommit, field } = setup()
    expect(field()).toHaveAttribute('aria-valuenow', '12')
    expect(field()).toHaveAttribute('aria-valuemin', '1')
    expect(field()).toHaveTextContent('$12')
    fireEvent.keyDown(field(), { key: 'ArrowRight' })
    expect(onCommit).toHaveBeenLastCalledWith(13)
    fireEvent.keyDown(field(), { key: 'ArrowLeft', shiftKey: true })
    expect(onCommit).toHaveBeenLastCalledWith(3)
  })

  it('refuses a step past a bound', () => {
    const { onCommit, field } = setup(60)
    fireEvent.keyDown(field(), { key: 'ArrowRight' })
    expect(onCommit).not.toHaveBeenCalled()
    expect(field()).toHaveClass('shake')
  })

  it('opens on click, commits typed text on Enter, and clamps with a shake', () => {
    const { onCommit, field } = setup()
    fireEvent.pointerDown(field(), { button: 0, clientX: 10 })
    fireEvent.pointerUp(field(), { clientX: 10 })
    const input = screen.getByRole('textbox', { name: 'Budget per case' })
    fireEvent.change(input, { target: { value: '99' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onCommit).toHaveBeenCalledTimes(1)
    expect(onCommit).toHaveBeenCalledWith(60)
    expect(field()).toHaveClass('shake')
  })

  it('drags to a new value and commits on release; Escape cancels typing', () => {
    const { onCommit, field } = setup()
    fireEvent.pointerDown(field(), { button: 0, clientX: 0 })
    fireEvent.pointerMove(field(), { clientX: 30 })
    fireEvent.pointerUp(field(), { clientX: 30 })
    expect(onCommit).toHaveBeenCalledTimes(1)
    expect(onCommit.mock.calls[0][0]).toBeGreaterThan(12)

    onCommit.mockClear()
    fireEvent.keyDown(field(), { key: 'Enter' })
    const input = screen.getByRole('textbox', { name: 'Budget per case' })
    fireEvent.change(input, { target: { value: '5' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onCommit).not.toHaveBeenCalled()
  })
})
