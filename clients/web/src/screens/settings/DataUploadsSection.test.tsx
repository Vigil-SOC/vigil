import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import DataUploadsSection from './DataUploadsSection'

vi.mock('./LoglmCard', () => ({ default: () => <section>LogLM card</section> }))
vi.mock('./DataIngestion', () => ({
  UploadCard: () => <section>Upload card</section>,
  StreamsCard: () => <section>Streams card</section>,
  DemoDataClear: () => <section>Demo card</section>,
}))
vi.mock('./DetectionRulesPanel', () => ({ default: () => <section>Rules card</section> }))

const scrollIntoView = vi.fn()

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <DataUploadsSection notify={vi.fn()} />
    </MemoryRouter>,
  )
}

describe('Data & uploads page', () => {
  beforeEach(() => {
    scrollIntoView.mockClear()
    Element.prototype.scrollIntoView = scrollIntoView
  })
  afterEach(() => {
    // @ts-expect-error jsdom has none
    delete Element.prototype.scrollIntoView
  })

  it('is one page: every card in board order, no tab strip', () => {
    renderAt('/settings?section=data')

    const text = document.body.textContent ?? ''
    const order = ['LogLM card', 'Upload card', 'Streams card', 'Rules card', 'Demo card', 'Retention']
    const at = order.map((name) => text.indexOf(name))
    expect(at.every((i) => i >= 0)).toBe(true)
    expect([...at].sort((a, b) => a - b)).toEqual(at)
    expect(document.querySelector('.tabs')).toBeNull()
    expect(scrollIntoView).not.toHaveBeenCalled()
  })

  it('scrolls the rule sources into view for ?tab=detection', () => {
    renderAt('/settings?section=data&tab=detection')

    expect(scrollIntoView).toHaveBeenCalled()
    const target = scrollIntoView.mock.contexts[0] as HTMLElement
    expect(target.id).toBe('rule-sources')
    expect(target).toHaveTextContent('Rules card')
  })

  it('says what retention does today, and that it is not measured', () => {
    renderAt('/settings?section=data')

    expect(screen.getByText(/Not measured yet/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /daemon_cleanup_retention_days/ }))
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent('No schedule deletes uploaded findings or cases')
    expect(tip).toHaveTextContent('episodic memory read log')
  })
})
