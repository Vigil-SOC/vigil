import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { auditApi, auditParams, type AuditRetention } from '../../services/api'
import { useAuth } from '../../contexts/AuthContext'
import AuditSection, { EMPTY_DRAFT, auditFilter, verifyLine } from './AuditSection'

const manager = { hasPermission: (p: string) => p === 'audit.manage' || p === 'audit.read' }

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: vi.fn(() => manager),
}))

vi.mock('../../services/api', async (orig) => {
  const real = await orig<typeof import('../../services/api')>()
  return {
    ...real,
    auditApi: {
      entries: vi.fn(() => Promise.resolve({ data: { entries: [], tip: { seq: 0, hash: 'h' } } })),
      download: vi.fn(() => Promise.resolve()),
      verify: vi.fn(),
      retention: vi.fn(() => Promise.resolve({
        data: { effective_days: null, pending_days: null, pending_on: null, unconfirmed: false, erasure_unconfirmed: false, erasures: [] },
      })),
      saveRetention: vi.fn(),
      erasePrincipal: vi.fn(),
    },
  }
})

const qs = (draft: Partial<typeof EMPTY_DRAFT>) => new URLSearchParams(auditParams(auditFilter({ ...EMPTY_DRAFT, ...draft }))).toString()

/** The card only reads `.data`. The cast satisfies tsc, which includes this file in `npm run build`. */
function retentionResponse(data: AuditRetention) {
  return { data } as unknown as Awaited<ReturnType<typeof auditApi.retention>>
}

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

  it('says when a restore entry is not yet off the box', async () => {
    const feed = { enabled: true, behind: 2, last_delivered_at: null, last_delivered_seq: 40, last_error: null, unconfirmed: 42, paused: true }
    vi.mocked(auditApi.entries).mockResolvedValueOnce({ data: { entries: [], tip: { seq: 42, hash: 'h' }, feed } } as never)
    render(<AuditSection notify={vi.fn()} />)
    expect(await screen.findByText(/restore entry \(seq 42\) has not been confirmed off the box/)).toBeTruthy()
    expect(screen.getByText(/New entries wait/)).toBeTruthy()
    expect(screen.getByText('2 entries behind')).toBeTruthy()
    expect(screen.queryByText(/no outside proof/)).toBeNull()
  })

  it('reads a verify result as ok or the first break', () => {
    expect(verifyLine({ first: 1, last: 9, first_bad: null, reason: '', ok: true })).toEqual({ level: 'good', text: 'Chain verified · seq 1–9' })
    expect(verifyLine({ first: 1, last: 9, first_bad: 4, reason: 'hash mismatch', ok: false }).text).toBe('Chain break at seq 4: hash mismatch')
  })
})

describe('retention card', () => {
  it('says the period could not load for a viewer rather than that none is in force', async () => {
    vi.mocked(useAuth).mockReturnValue({ hasPermission: (p: string) => p === 'audit.read' } as unknown as ReturnType<typeof useAuth>)
    vi.mocked(auditApi.retention).mockRejectedValueOnce(new Error('network down'))
    try {
      render(<AuditSection notify={vi.fn()} />)
      expect(await screen.findByText('Could not load the retention period.')).toBeTruthy()
      expect(screen.queryByText('No retention period is in force yet.')).toBeNull()
    } finally {
      vi.mocked(useAuth).mockReturnValue(manager as unknown as ReturnType<typeof useAuth>)
    }
  })

  it('keeps a saved announcement when the earlier load arrives late', async () => {
    vi.mocked(useAuth).mockReturnValue(manager as unknown as ReturnType<typeof useAuth>)
    let resolveLoad: (value: Awaited<ReturnType<typeof auditApi.retention>>) => void = () => {}
    vi.mocked(auditApi.retention).mockReturnValueOnce(new Promise((resolve) => {
      resolveLoad = resolve
    }))
    vi.mocked(auditApi.saveRetention).mockResolvedValueOnce(retentionResponse({
      effective_days: null,
      pending_days: 365,
      pending_on: '2026-11-02T21:07:14Z',
      unconfirmed: true,
      erasure_unconfirmed: false,
      erasures: [],
    }))
    render(<AuditSection notify={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('This announcement has not left the box.')).toBeTruthy()
    expect(screen.getByText(/365 days takes effect/)).toBeTruthy()
    await act(async () => {
      resolveLoad(retentionResponse({
        effective_days: null,
        pending_days: null,
        pending_on: null,
        unconfirmed: false,
        erasure_unconfirmed: false,
        erasures: [],
      }))
    })
    expect(screen.getByText('This announcement has not left the box.')).toBeTruthy()
    expect(screen.getByText(/365 days takes effect/)).toBeTruthy()
    expect(screen.queryByText('Could not load the retention period.')).toBeNull()
  })

  it('does not turn a saved period into a load failure when the earlier load rejects', async () => {
    vi.mocked(useAuth).mockReturnValue(manager as unknown as ReturnType<typeof useAuth>)
    let rejectLoad: (reason: Error) => void = () => {}
    vi.mocked(auditApi.retention).mockReturnValueOnce(new Promise((_, reject) => {
      rejectLoad = reject
    }) as ReturnType<typeof auditApi.retention>)
    vi.mocked(auditApi.saveRetention).mockResolvedValueOnce(retentionResponse({
      effective_days: 365,
      pending_days: null,
      pending_on: null,
      unconfirmed: false,
      erasure_unconfirmed: false,
      erasures: [],
    }))
    render(<AuditSection notify={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('The trail is kept for 365 days.')).toBeTruthy()
    await act(async () => {
      rejectLoad(new Error('late'))
    })
    expect(screen.getByText('The trail is kept for 365 days.')).toBeTruthy()
    expect(screen.queryByText('Could not load the retention period.')).toBeNull()
  })
})
