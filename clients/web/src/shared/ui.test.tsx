import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Popup, Select } from './ui'

function NestedLayers({ onParentClose }: { onParentClose: () => void }) {
  const [open, setOpen] = useState(true)

  return (
    <Popup
      open={open}
      title={<span>Review finding</span>}
      onClose={() => {
        setOpen(false)
        onParentClose()
      }}
    >
      <Select
        value=""
        placeholder="Choose status"
        options={[{ value: 'open', label: 'Open' }]}
        onSelect={() => undefined}
      />
    </Popup>
  )
}

describe('shared UI', () => {
  it('names dialogs from the visible heading and labels the close action', () => {
    render(<Popup open title={<span>Case confirmation</span>} onClose={() => undefined}>Body</Popup>)

    expect(screen.getByRole('dialog', { name: 'Case confirmation' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument()
  })

  it('dismisses only the topmost Select before its parent Popup', () => {
    const onParentClose = vi.fn()
    render(<NestedLayers onParentClose={onParentClose} />)

    fireEvent.click(screen.getByRole('button', { name: 'Choose status' }))
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Review finding' })).toBeInTheDocument()
    expect(onParentClose).not.toHaveBeenCalled()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onParentClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog', { name: 'Review finding' })).not.toBeInTheDocument()
  })

  describe('searchable Select', () => {
    const models = [
      { value: 'gemini-pro', label: 'Gemini Pro' },
      { value: 'gemini-flash', label: 'Gemini Flash' },
      { value: 'claude-opus', label: 'Claude Opus' },
    ]
    const setup = (onSelect = vi.fn()) => {
      render(<Select searchable value="" placeholder="Pick model" options={models} onSelect={onSelect} />)
      fireEvent.click(screen.getByRole('button', { name: 'Pick model' }))
      return { onSelect, input: screen.getByRole('combobox') }
    }

    it('filters options by label or value as you type and shows an empty row', () => {
      const { input } = setup()
      expect(input).toHaveFocus()

      fireEvent.change(input, { target: { value: 'ge' } })
      expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['Gemini Pro', 'Gemini Flash'])

      fireEvent.change(input, { target: { value: 'OPUS' } })
      expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['Claude Opus'])

      fireEvent.change(input, { target: { value: 'zzz' } })
      expect(screen.queryAllByRole('option')).toHaveLength(0)
      expect(screen.getByText('No matches')).toBeInTheDocument()
    })

    it('selects with ArrowDown and Enter, then closes', () => {
      const { onSelect } = setup()
      fireEvent.keyDown(document, { key: 'ArrowDown' })
      fireEvent.keyDown(document, { key: 'Enter' })
      expect(onSelect).toHaveBeenCalledWith('gemini-flash')
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    })

    it('closes only the Select on Escape inside a Popup', () => {
      const onParentClose = vi.fn()
      render(
        <Popup open title="Review" onClose={onParentClose}>
          <Select searchable value="" placeholder="Pick model" options={models} onSelect={() => undefined} />
        </Popup>,
      )
      fireEvent.click(screen.getByRole('button', { name: 'Pick model' }))
      fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
      expect(onParentClose).not.toHaveBeenCalled()
    })
  })

  it('moves through a plain Select with the arrow keys', () => {
    const onSelect = vi.fn()
    render(<Select value="b" options={[{ value: 'a', label: 'A' }, { value: 'b', label: 'B' }, { value: 'c', label: 'C' }]} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole('button', { name: 'B' }))
    expect(screen.getByRole('option', { name: 'B' })).toHaveClass('active')
    fireEvent.keyDown(document, { key: 'End' })
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('c')
  })

  it('restores focus to the element that opened the Popup', () => {
    function FocusExample() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button onClick={() => setOpen(true)}>Open review</button>
          <Popup open={open} title="Review" onClose={() => setOpen(false)}>Body</Popup>
        </>
      )
    }

    render(<FocusExample />)
    const opener = screen.getByRole('button', { name: 'Open review' })
    opener.focus()
    fireEvent.click(opener)
    expect(screen.getByRole('dialog', { name: 'Review' })).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(opener).toHaveFocus()
  })

  it('preserves outside-click dismissal', () => {
    const onClose = vi.fn()
    render(<Popup open title="Review" onClose={onClose}>Body</Popup>)

    fireEvent.click(screen.getByRole('dialog', { name: 'Review' }))
    expect(onClose).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('dialog', { name: 'Review' }).parentElement as HTMLElement)
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
