import { describe, it, expect, vi, beforeEach } from 'vitest'
import axios from 'axios'

// Mock axios
vi.mock('axios')

describe('API Utils', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('should make GET request', async () => {
    const mockData = { id: '123', title: 'Test Case' }
    ;(axios.get as any).mockResolvedValue({ data: mockData })

    const response = await axios.get('/api/cases/123')
    
    expect(response.data).toEqual(mockData)
    expect(axios.get).toHaveBeenCalledWith('/api/cases/123')
  })

  it('should handle errors', async () => {
    (axios.get as any).mockRejectedValue(new Error('Network error'))

    await expect(axios.get('/api/cases/123')).rejects.toThrow('Network error')
  })
})

