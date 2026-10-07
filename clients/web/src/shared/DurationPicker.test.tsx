import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { DurationPicker } from './DurationPicker'
import { formatDuration } from './duration'

function setup(initial = 1.5) {
  const onChange = vi.fn()
  function Host() {
    const [v, setV] = useState(initial)
    return <DurationPicker label="Respond within" value={v} onChange={(h) => { onChange(h); setV(h) }} />
  }
  render(<Host />)
  const pill = () => screen.getByRole('button', { name: /^Respond within:/ })
  const open = () => fireEvent.click(pill())
  return { onChange, pill, open }
}

describe('formatDuration', () => {
  it('words whole hours, minutes, and both', () => {
    expect(formatDuration(1)).toBe('1 hour')
    expect(formatDuration(4)).toBe('4 hours')
    expect(formatDuration(0.75)).toBe('45 min')
    expect(formatDuration(1.5)).toBe('1 hour 30 min')
  })
})

describe('DurationPicker', () => {
  it('shows the stored hours as h and min, and opens a popover on click', () => {
    const { pill, open } = setup(2)
    expect(pill()).toHaveTextContent('2 h 00 min')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    open()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByLabelText('Hours')).toHaveValue('2')
  })

  it('writes typed hours and minutes back as float hours', () => {
    const { onChange, open } = setup(1)
    open()
    fireEvent.change(screen.getByLabelText('Minutes'), { target: { value: '30' } })
    expect(onChange).toHaveBeenLastCalledWith(1.5)
    fireEvent.change(screen.getByLabelText('Hours'), { target: { value: '3' } })
    expect(onChange).toHaveBeenLastCalledWith(3.5)
  })

  it('clamps minutes to 59 and refuses a zero-length duration, shaking both times', () => {
    const { onChange, open } = setup(1)
    open()
    const fields = () => document.querySelector('.dp-fields')!
    fireEvent.change(screen.getByLabelText('Minutes'), { target: { value: '75' } })
    expect(onChange).toHaveBeenLastCalledWith(1 + 59 / 60)
    expect(fields()).toHaveClass('shake')
    fireEvent.animationEnd(fields())
    expect(fields()).not.toHaveClass('shake')
    onChange.mockClear()
    fireEvent.change(screen.getByLabelText('Minutes'), { target: { value: '0' } })
    fireEvent.change(screen.getByLabelText('Hours'), { target: { value: '0' } })
    expect(onChange).toHaveBeenLastCalledWith(1) // the last valid value stands
    expect(fields()).toHaveClass('shake')
  })

  it('presets set the duration', () => {
    const { onChange, open } = setup(1)
    open()
    fireEvent.click(screen.getByRole('button', { name: '15 min' }))
    expect(onChange).toHaveBeenLastCalledWith(0.25)
    fireEvent.click(screen.getByRole('button', { name: '4 h' }))
    expect(onChange).toHaveBeenLastCalledWith(4)
  })

  it('arrows step hours by 1 and minutes by 5, and stop at one minute', () => {
    const { onChange, open } = setup(1)
    open()
    fireEvent.keyDown(screen.getByLabelText('Hours'), { key: 'ArrowUp' })
    expect(onChange).toHaveBeenLastCalledWith(2)
    fireEvent.keyDown(screen.getByLabelText('Minutes'), { key: 'ArrowUp' })
    expect(onChange).toHaveBeenLastCalledWith(2 + 5 / 60)
    for (const p of ['15 min']) fireEvent.click(screen.getByRole('button', { name: p }))
    fireEvent.keyDown(screen.getByLabelText('Minutes'), { key: 'ArrowDown' })
    fireEvent.keyDown(screen.getByLabelText('Minutes'), { key: 'ArrowDown' })
    fireEvent.keyDown(screen.getByLabelText('Minutes'), { key: 'ArrowDown' })
    fireEvent.keyDown(screen.getByLabelText('Minutes'), { key: 'ArrowDown' })
    expect(onChange).toHaveBeenLastCalledWith(1 / 60)
  })

  it('Enter and Escape close the popover and return focus to the pill', () => {
    const { pill, open } = setup(1)
    open()
    fireEvent.keyDown(screen.getByLabelText('Hours'), { key: 'Enter' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(pill()).toHaveFocus()
    open()
    fireEvent.keyDown(screen.getByLabelText('Minutes'), { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
