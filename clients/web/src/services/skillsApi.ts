import api from './api'

/**
 * Skills API. The list is loaded from disk. Saves and deletes go to the
 * operator root (`VIGIL_SKILLS_PATH`); the bundled library is never written.
 */

/** Wire row from GET /api/skills: a skill loaded from disk (#928, #1387). */
export interface ApiSkill {
  name: string
  description: string
  source_path: string
  bundled: boolean
}

/** GET /api/skills/{name}: the Markdown body, without frontmatter. */
export interface ApiSkillDetail extends ApiSkill {
  body: string
  operator_root_set: boolean
}

export interface SkillWrite {
  name: string
  description: string
  body: string
}

export const skillsApi = {
  list: () => api.get<ApiSkill[]>('/skills').then((r) => r.data),
  get: (name: string) => api.get<ApiSkillDetail>(`/skills/${encodeURIComponent(name)}`).then((r) => r.data),
  save: (skill: SkillWrite) => api.post<ApiSkill>('/skills', skill).then((r) => r.data),
  delete: (name: string) => api.delete(`/skills/${encodeURIComponent(name)}`).then((r) => r.data),
}
