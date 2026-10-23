import { describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { attackApi } from '../../services/api'
import { useAttack } from './useAttack'

vi.mock('../../services/api', () => ({
  default: {},
  attackApi: { getTechniqueRollup: vi.fn() },
}))

describe('useAttack', () => {
  it('shows the names the rollup resolved and a dash where it could not', async () => {
    vi.mocked(attackApi.getTechniqueRollup).mockResolvedValue({
      data: {
        techniques: [
          { technique_id: 'T1573.001', technique_name: 'Encrypted Channel', tactic: 'Command and Control', count: 2, severities: { high: 2 } },
          { technique_id: 'T9999', technique_name: 'T9999', tactic: 'Unknown', count: 1, severities: { low: 1 } },
        ],
      },
    } as never)
    const { result } = renderHook(() => useAttack(0, 'All', ''))
    await waitFor(() => expect(result.current.phase).toBe('ready'))

    expect(result.current.data?.techniques.map((t) => [t.id, t.name, t.tactic])).toEqual([
      ['T1573.001', 'Encrypted Channel', 'Command and Control'],
      ['T9999', '—', '—'],
    ])
    expect(result.current.data?.tacticDist).toEqual([['Command and Control', 2], ['—', 1]])
  })
})
