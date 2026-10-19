import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { useRef } from 'react'
import { CaseSide } from './CaseSide'
import { casesApi } from '../../services/api'
import { useNoSideways } from '../../shared/useNoSideways'
import type { RunFold } from './caseFold'
import casesCss from './cases.css?inline'

vi.mock('../../services/api', () => ({
  casesApi: {
    getComments: vi.fn(),
    getTasks: vi.fn(),
    getEscalations: vi.fn(),
  },
}))

const SHA_A = '4b7d9e2f1a3c5e7091b2d4f6a8c0e1f3a5b7d9e1f2a4c6e8b0d2f4a6c8e0b1d3'
const SHA_B = '9f2c4e1a7b3d5f60e8a1c2b4d6f8091a3c5e7f9b1d2e4f6a8c0b2d4e6f8a0c1e'
const KEYS = ['domain:cdn-invoicesync.net', `hash:${SHA_A}`, `hash:${SHA_B}`, 'ip:203.0.113.47']

/** A hunt whose recall asked about two hashes and found nothing: the rehearsal's side panel (#2284). */
const fold = {
  kind: 'hunt',
  run: { status: 'running', error: '', reason: '', huntStatus: '' },
  iteration: 8,
  doing: '',
  worker: '',
  moves: [],
  hypotheses: [],
  evidence: [],
  evidenceCount: 0,
  calls: [],
  recall: { keys: KEYS, gaps: [], sightings: [], verdicts: [], unavailable: null },
  outcome: null,
  reason: '',
  costUsd: null,
  maxCostUsd: null,
} as unknown as RunFold

function renderSide({ keys = KEYS, entities = ['FIN-WS-114', 'priya.nair'] }: { keys?: string[]; entities?: string[] } = {}) {
  return render(
    <aside className="case-side">
      <CaseSide
        caseId="case-1"
        linked={[{ finding_id: 'dl-001', title: 'ISO delivered', description: null, source_link: null, entities }]}
        owner="—"
        latest={null}
        workflowNames={{}}
        sla={null}
        closed={false}
        fold={{ ...fold, recall: { ...(fold as unknown as { recall: object }).recall, keys } } as unknown as RunFold}
        latestFold={null}
      />
    </aside>,
  )
}

beforeEach(() => {
  vi.mocked(casesApi.getComments).mockResolvedValue({ data: { comments: [] } } as never)
  vi.mocked(casesApi.getTasks).mockResolvedValue({ data: { tasks: [] } } as never)
  vi.mocked(casesApi.getEscalations).mockResolvedValue({ data: { escalations: [] } } as never)
})

describe('case side panel Entities (#2284)', () => {
  it('shows the first three, then +N that opens the rest', () => {
    renderSide()
    const shown = () => Array.from(document.querySelectorAll('.side-ent')).map((el) => el.getAttribute('title'))
    expect(shown()).toEqual(['domain:cdn-invoicesync.net', `hash:${SHA_A}`, `hash:${SHA_B}`])
    fireEvent.click(screen.getByRole('button', { name: '+3' }))
    expect(shown()).toEqual([...KEYS, 'FIN-WS-114', 'priya.nair'])
    fireEvent.click(screen.getByRole('button', { name: 'Show fewer' }))
    expect(shown()).toHaveLength(3)
  })

  it('cuts a long hash in the middle on screen while the whole value stays in the DOM to copy', () => {
    renderSide()
    const hash = document.querySelector(`.side-ent[title="hash:${SHA_A}"]`) as HTMLElement
    expect(hash.textContent).toBe(SHA_A)
    expect(within(hash).getByText(SHA_A.slice(-6))).toHaveClass('side-ent-tail')
    // a short value is not split
    fireEvent.click(screen.getByRole('button', { name: '+3' }))
    expect(document.querySelector('.side-ent[title="FIN-WS-114"] .side-ent-tail')).toBeNull()
  })
})

describe('case side panel Entities show values that fit whole (#2459)', () => {
  it('shows a domain whole, without its kind, which stays on hover', () => {
    renderSide()
    const domain = document.querySelector('.side-ent[title="domain:cdn-invoicesync.net"]') as HTMLElement
    expect(domain.textContent).toBe('cdn-invoicesync.net')
    expect(domain).not.toHaveClass('long')
    expect(domain.querySelector('.side-ent-tail')).toBeNull()
  })

  it('lists a value once when a recall key and an alert both name it', () => {
    renderSide({ keys: ['host:FIN-WS-114'], entities: ['FIN-WS-114'] })
    expect(Array.from(document.querySelectorAll('.side-ent')).map((el) => el.textContent)).toEqual(['FIN-WS-114'])
  })
})

describe('case side panel never scrolls sideways (#2284)', () => {
  it('holds the Known line that names every entity to two lines, the whole of it on hover', () => {
    renderSide()
    const line = screen.getByText(/^Nothing recorded about /)
    expect(line).toHaveClass('side-clamp')
    expect(line).toHaveAttribute('title', `Nothing recorded about ${KEYS.join(', ')}.`)
  })

  it('wraps any long token in the panel rather than widening it, and cuts an entity with an ellipsis', () => {
    const rule = (selector: string) => {
      const at = casesCss.indexOf(`${selector} {`)
      expect(at, `${selector} rule`).toBeGreaterThanOrEqual(0)
      return casesCss.slice(at, casesCss.indexOf('}', at))
    }
    expect(rule('.case-side')).toMatch(/overflow-wrap:\s*anywhere/)
    expect(rule('.side-ent-head')).toMatch(/text-overflow:\s*ellipsis/)
    expect(rule('.side-ent')).toMatch(/max-width:\s*100%/)
    // a flex entity would copy as two lines, head and tail
    expect(rule('.side-ent')).toMatch(/display:\s*block/)
  })
})

describe('useNoSideways', () => {
  let observed: (() => void) | null = null
  beforeEach(() => {
    observed = null
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: () => void) {
          observed = cb
        }
        observe() {}
        disconnect() {}
      },
    )
  })
  afterEach(() => vi.unstubAllGlobals())

  function Panel() {
    const ref = useRef<HTMLElement>(null)
    useNoSideways(ref, 'The test panel')
    return <aside ref={ref} data-testid="panel"><p>text</p></aside>
  }

  it('reports a panel whose scrollWidth passes its clientWidth, once until it fits again', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    render(<Panel />)
    const panel = screen.getByTestId('panel')
    let scroll = 269
    Object.defineProperty(panel, 'clientWidth', { configurable: true, get: () => 269 })
    Object.defineProperty(panel, 'scrollWidth', { configurable: true, get: () => scroll })
    observed?.()
    expect(error).not.toHaveBeenCalled()

    scroll = 541
    observed?.()
    observed?.()
    expect(error).toHaveBeenCalledTimes(1)
    expect(String(error.mock.calls[0][0])).toMatch(/The test panel scrolls sideways \(scrollWidth 541 > clientWidth 269\)/)

    scroll = 269
    observed?.()
    scroll = 600
    observed?.()
    expect(error).toHaveBeenCalledTimes(2)
    error.mockRestore()
  })
})
