import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import DashboardScreen from './DashboardScreen'
import { ToastProvider } from '../../shell/toast'
import type { Finding } from '../../data/data'

const state = vi.hoisted(() => ({ rows: [] as Finding[] }))
vi.mock('../../extensions/ExtensionProvider', () => ({ useExtensions: () => ({ enabledIntegrations: ['vstrike'], loading: false, extensions: [], mountPoints: [], reload: vi.fn() }) }))
vi.mock('./useFindings', () => ({
  useFindings: () => ({ rows: state.rows, phase: 'ready', reload: vi.fn() }),
  useDashboardKpis: () => ({ kpis: { findingsTotal: 41, findingsCritical: 30, findingsHigh: 10, casesTotal: 9, casesOpen: 2, casesInvestigating: 1 }, reload: vi.fn() }),
}))
vi.mock('./FindingPopup', () => ({ default: ({ id, onClose }: { id: string | null; onClose: () => void }) => id ? <section role="dialog" aria-label="Finding detail">{id}<button onClick={onClose}>Close finding</button></section> : null }))
vi.mock('../../integrations/vstrike/VStrikePanel', () => ({ default: ({ active, request, onBack, onBackToFinding }: { active: boolean; request: { findingId: string }; onBack: () => void; onBackToFinding: (id: string) => void }) => active ? <section>Requested graph<button onClick={onBack}>Back to results</button><button onClick={() => onBackToFinding(request.findingId)}>Back to finding</button></section> : null }))
const go = vi.fn()
function mount() { return render(<MemoryRouter><ToastProvider><DashboardScreen openChat={vi.fn()} go={go} goSettings={vi.fn()} setViewFull={vi.fn()} /></ToastProvider></MemoryRouter>) }

beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks()
  state.rows = Array.from({ length: 40 }, (_, index) => ({ id: `synthetic-${index}`, title: `Example finding ${index}`, sev: index < 30 ? 'Critical' : 'High', tech: '—', conf: 0, tactic: '—', src: 'flow', host: '—', user: '—', time: '', ts: index, score: index / 40, status: 'open', sourceIp: '192.0.2.2', destinationIp: '198.51.100.10' }))
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.setAttribute('open', '') } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.removeAttribute('open') } })
})

describe('findings queue navigation', () => {
  it('keeps filters, sort, page, and scroll through detail and graph', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Filter critical findings' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Search findings' }), { target: { value: '192.0.2.2' } })
    fireEvent.click(screen.getByRole('columnheader', { name: 'Score' }))
    fireEvent.click(screen.getByTitle('Next page'))
    expect(screen.getByText('11–20 of 30')).toBeVisible()
    const table = screen.getByRole('table').parentElement!
    table.scrollTop = 100; table.scrollLeft = 90; fireEvent.scroll(table)
    fireEvent.click(screen.getByRole('button', { name: 'Example finding 19' }))
    fireEvent.click(screen.getByRole('button', { name: 'Close finding' }))
    fireEvent.click(screen.getByRole('button', { name: 'View synthetic-19 endpoints in VStrike' }))
    expect(screen.getByText('Requested graph')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Back to finding' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('synthetic-19')
    fireEvent.click(screen.getByRole('button', { name: 'Close finding' }))
    expect(screen.getByText('11–20 of 30')).toBeVisible()
    expect(screen.getByRole('textbox', { name: 'Search findings' })).toHaveValue('192.0.2.2')
    expect(screen.getByText('Severity: critical ×')).toBeVisible()
    expect(table.scrollTop).toBe(100); expect(table.scrollLeft).toBe(90)
  })
  it('separates columns from filtering and retains display preferences when clearing filters', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Columns' }))
    const drawer = screen.getByRole('dialog', { name: 'Columns' })
    fireEvent.click(within(drawer).getByRole('checkbox', { name: 'Host' }))
    fireEvent(drawer, new Event('cancel', { bubbles: true }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Filter critical findings' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear all filters' }))
    expect(screen.getByRole('columnheader', { name: 'Host' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Filters' }))
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })
  it('counts active cases without closed cases and opens the cases screen', () => {
    mount()
    const button = screen.getByRole('button', { name: 'Open active cases' })
    expect(within(button).getByText('3')).toBeVisible()
    fireEvent.click(button)
    expect(go).toHaveBeenCalledWith('cases')
  })
  it('disables the inline action when there is no complete endpoint pair', () => {
    state.rows[39].destinationIp = undefined
    mount()
    expect(screen.getByRole('button', { name: 'View synthetic-39 endpoints in VStrike' })).toBeDisabled()
  })
})
