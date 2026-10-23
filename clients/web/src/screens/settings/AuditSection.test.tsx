import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { auditApi, auditParams } from '../../services/api'
import AuditSection, { EMPTY_DRAFT, auditFilter, verifyLine } from './AuditSection'

vi.mock('../../services/api', async (orig) => {
  const real = await orig<typeof import('../../services/api')>()
  return {
    ...real,
    auditApi: {
      entries: vi.fn(() => Promise.resolve({ data: { entries: [], tip: { seq: 0, hash: 'h' } } })),
      download: vi.fn(() => Promise.resolve()),
      verify: vi.fn(),
    },
  }
})

const qs = (draft: Partial<typeof EMPTY_DRAFT>) => new URLSearchParams(auditParams(auditFilter({ ...EMPTY_DRAFT, ...draft }))).toString()

describe('audit filter query string', () => {
  it('leaves out empty params and trims the rest', () => {
    expect(qs({})).toBe('')
    expect(qs({ actor: '  ', object: ' case-1 ', area: 'config' })).toBe('area=config&object=case-1')
  })

  it('sends outcome and attribution in the spaced spelling the trail accepts', () => {
    expect(qs({ outcome: 'outcome unknown', attribution: 'matched by name', kind: 'agent' }))
      .toBe('kind=agent&outcome=outcome+unknown&attribution=matched+by+name')
  })

  it('sends since and until as UTC ISO stamps', () => {
    const since = '2026-10-01T09:30'
    expect(auditParams(auditFilter({ ...EMPTY_DRAFT, since })).since).toBe(new Date(since).toISOString())
    expect(auditParams(auditFilter({ ...EMPTY_DRAFT, until: 'not a date' }))).toEqual({})
  })

  it('reads the case export handoff as the object filter', () => {
    expect(new URLSearchParams(auditParams({ object: 'CASE-7' })).toString()).toBe('object=CASE-7')
  })
})

describe('audit section', () => {
  it('lists and exports under the same filter', async () => {
    render(<AuditSection notify={vi.fn()} />)
    await waitFor(() => expect(auditApi.entries).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Recorded late' }))
    const want = auditFilter({ ...EMPTY_DRAFT, outcome: 'recorded late' })
    await waitFor(() => expect(auditApi.entries).toHaveBeenLastCalledWith(want, undefined))
    fireEvent.click(screen.getByRole('button', { name: /Export/ }))
    await waitFor(() => expect(auditApi.download).toHaveBeenCalledWith(want))
  })

  it('reads a verify result as ok or the first break', () => {
    expect(verifyLine({ first: 1, last: 9, first_bad: null, reason: '', ok: true })).toEqual({ level: 'good', text: 'Chain verified · seq 1–9' })
    expect(verifyLine({ first: 1, last: 9, first_bad: 4, reason: 'hash mismatch', ok: false }).text).toBe('Chain break at seq 4: hash mismatch')
  })
})
