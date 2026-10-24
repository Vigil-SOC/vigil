import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

const gets = vi.hoisted(() => [] as Array<{ url: string; params?: Record<string, string> }>)

vi.mock('axios', () => {
  const instance = {
    get: vi.fn((url: string, config?: { params?: Record<string, string> }) => {
      gets.push({ url, params: config?.params })
      return Promise.resolve({ data: {} })
    }),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  const axios = { create: () => instance, isAxiosError: () => false }
  return { default: axios, ...axios }
})

import MetricsScreen from './MetricsScreen'

const READS = ['summary', 'mttd', 'mttr', 'by-priority', 'by-status', 'analyst-performance'].map(
  (r) => `/cases/metrics/${r}`,
)

describe('MetricsScreen time range', () => {
  it('sends the selected window to every metrics read', async () => {
    render(<MetricsScreen />)
    await waitFor(() => expect(gets).toHaveLength(READS.length))
    gets.length = 0

    fireEvent.click(screen.getByRole('button', { name: '7d' }))
    await waitFor(() => expect(gets).toHaveLength(READS.length))

    expect(gets.map((g) => g.url).sort()).toEqual([...READS].sort())
    for (const { url, params } of gets) {
      const days = (Date.parse(params!.end_date) - Date.parse(params!.start_date)) / 864e5
      expect(days, url).toBe(7)
    }
  })
})
