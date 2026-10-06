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
  /** Regular files in the skill folder, SKILL.md included; same list the drawer shows. */
  file_count: number
}

/** A file in the skill folder: posix path relative to it, and size in bytes. */
export interface ApiSkillFile {
  path: string
  size: number
}

/** GET /api/skills/{name}: the Markdown body, without frontmatter, the folder's files and the version. */
export interface ApiSkillDetail extends ApiSkill {
  body: string
  operator_root_set: boolean
  version: number
  files: ApiSkillFile[]
}

export interface SkillWrite {
  name: string
  description: string
  body: string
  /** A loaded skill whose whole folder is copied under the new name. */
  source?: string
  /** The version the editor opened; an overwrite is refused (409) if it has moved. */
  version?: number
}

export const skillsApi = {
  list: () => api.get<ApiSkill[]>('/skills').then((r) => r.data),
  get: (name: string) => api.get<ApiSkillDetail>(`/skills/${encodeURIComponent(name)}`).then((r) => r.data),
  file: (name: string, path: string) =>
    api
      .get<{ path: string; content: string }>(
        `/skills/${encodeURIComponent(name)}/files/${path.split('/').map(encodeURIComponent).join('/')}`,
      )
      .then((r) => r.data),
  save: (skill: SkillWrite) => api.post<ApiSkill>('/skills', skill).then((r) => r.data),
  delete: (name: string) => api.delete(`/skills/${encodeURIComponent(name)}`).then((r) => r.data),
}
