import axios from 'axios'

/**
 * Client for the Skills API (GitHub issue #82).
 *
 * Skills are reusable, parameterized SOC capabilities that agents/workflows
 * will eventually invoke. The MVP surface is CRUD + AI-assisted generation.
 */

export const SKILL_CATEGORIES = [
  'detection',
  'enrichment',
  'response',
  'reporting',
  'custom',
] as const

export type SkillCategory = typeof SKILL_CATEGORIES[number]

export interface SkillDraft {
  name: string
  description?: string
  category: SkillCategory
  input_schema: Record<string, any>
  output_schema: Record<string, any>
  required_tools: string[]
  prompt_template: string
  execution_steps: Record<string, any>[]
  is_active?: boolean
}

export interface Skill extends SkillDraft {
  skill_id: string
  created_by?: string | null
  version: number
  created_at?: string | null
  updated_at?: string | null
}

export interface SkillGenerateRequest {
  description: string
  category?: SkillCategory
  conversation_history?: { role: string; content: string }[] | null
  user_response?: string | null
}

export interface SkillGenerateResponse {
  success: boolean
  needs_clarification: boolean
  message?: string
  conversation_history?: { role: string; content: string }[]
  skill?: SkillDraft
  error?: string
}

export interface SkillImportResult {
  skill_id: string
  name: string
  version: number
  replaced: boolean
}

const client = axios.create({
  baseURL: '/api/skills',
  headers: { 'Content-Type': 'application/json' },
})

client.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token && config.headers) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

export const skillsApi = {
  generate: (body: SkillGenerateRequest) =>
    client.post<SkillGenerateResponse>('/generate', body).then((r) => r.data),

  list: (params?: { category?: SkillCategory; is_active?: boolean }) =>
    client.get<Skill[]>('', { params }).then((r) => r.data),

  get: (skillId: string) =>
    client.get<Skill>(`/${skillId}`).then((r) => r.data),

  create: (body: SkillDraft & { created_by?: string }) =>
    client.post<Skill>('', body).then((r) => r.data),

  update: (skillId: string, patch: Partial<SkillDraft>) =>
    client.put<Skill>(`/${skillId}`, patch).then((r) => r.data),

  remove: (skillId: string) =>
    client.delete<{ success: boolean; skill_id: string }>(`/${skillId}`).then((r) => r.data),

  importZip: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return client
      .post<SkillImportResult>('/import', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data)
  },
}
