import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import MetricsScreen from './MetricsScreen'
import { caseMetricsApi } from '../../services/api'

vi.mock('../../services/api', () => ({
  caseMetricsApi: {
    getSummary: vi.fn(),
    getMTTD: vi.fn(),
    getMTTR: vi.fn(),
    getByPriority: vi.fn(),
    getByStatus: vi.fn(),
    getAnalystPerformance: vi.fn(),
  },
}))

function serve(mttdHours: number | null, mttrHours: number | null) {
  const m = vi.mocked(caseMetricsApi)
  m.getSummary.mockResolvedValue({ data: { total_cases: 3, open_cases: 3, critical_cases: 0, resolved_cases: 0 } } as never)
  m.getMTTD.mockResolvedValue({ data: { total_cases: 3, average_mttd_hours: mttdHours, mttd_by_priority: {} } } as never)
  m.getMTTR.mockResolvedValue({ data: { total_cases: 0, average_mttr_hours: mttrHours, mttr_by_priority: {} } } as never)
  m.getByPriority.mockResolvedValue({ data: { priority_breakdown: [{ priority: 'high', count: 3, closed_count: 0 }] } } as never)
  m.getByStatus.mockResolvedValue({ data: { status_breakdown: [{ status: 'open', count: 3 }] } } as never)
  m.getAnalystPerformance.mockResolvedValue({ data: { analyst_performance: [] } } as never)
}

async function tiles() {
  const mttd = (await screen.findByText('MTTD')).parentElement!.textContent
  const mttr = screen.getByText('MTTR').parentElement!.textContent
  return { mttd, mttr }
}

describe('MetricsScreen MTTD/MTTR tiles', () => {
  it('shows a dash when the backend measured nothing', async () => {
    serve(null, null)
    render(<MetricsScreen />)
    const { mttd, mttr } = await tiles()
    expect(mttd).toBe('MTTD—mean time to detect')
    expect(mttr).toBe('MTTR—mean time to resolve')
  })

  it('shows the measured mean', async () => {
    serve(0.5, 30)
    render(<MetricsScreen />)
    const { mttd, mttr } = await tiles()
    expect(mttd).toBe('MTTD30mmean time to detect')
    expect(mttr).toBe('MTTR1.3dmean time to resolve')
  })
})
