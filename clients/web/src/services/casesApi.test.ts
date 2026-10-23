import { beforeEach, describe, expect, it, vi } from 'vitest'

const post = vi.hoisted(() => vi.fn())
vi.mock('axios', () => ({
  default: { create: () => ({ post, interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }) },
}))

import { casesApi } from './api'

describe('casesApi.exportJira', () => {
  beforeEach(() => post.mockReset())

  it('posts the project key to the case export and names the created issue', async () => {
    post.mockResolvedValueOnce({ data: { success: true, issue_key: 'SOC-12', subtasks_created: 0 } })
    await expect(casesApi.exportJira('case 1', 'SOC')).resolves.toBe('Created SOC-12')
    expect(post).toHaveBeenCalledWith('/cases/case%201/export/jira', { project_key: 'SOC' })
  })

  it('says a ticket was created when Jira sends no issue key', async () => {
    post.mockResolvedValueOnce({ data: { success: true, subtasks_created: 0 } })
    await expect(casesApi.exportJira('case-1', 'SOC')).resolves.toBe('Ticket created')
  })

  it('throws the server error when the export is refused', async () => {
    post.mockResolvedValueOnce({ data: { success: false, error: 'JIRA API error: HTTP 401', subtasks_created: 0 } })
    await expect(casesApi.exportJira('case-1', 'SOC')).rejects.toThrow('JIRA API error: HTTP 401')
    post.mockResolvedValueOnce({ data: { success: false, error: null, subtasks_created: 0 } })
    await expect(casesApi.exportJira('case-1', 'SOC')).rejects.toThrow('Jira export failed')
  })
})
