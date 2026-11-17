import api, { LLM_TIMEOUT } from './api'

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
  /** Reads of the skill body in the last 7 days, and the distinct agents that made them. Absent from an API that predates usage recording. */
  reads_7d?: number
  agents_7d?: number
  /** Where the skill's content scan stands. Held skills are kept from agents. Absent from an API that predates scanning. */
  scan?: ApiSkillScan
}

/** bundled, unchecked (scanning off), safe and approved reach agents; pending, flagged, failed and refused are held. */
export type SkillScanState = 'bundled' | 'unchecked' | 'pending' | 'safe' | 'flagged' | 'failed' | 'refused' | 'approved'

export interface ApiSkillScan {
  state: SkillScanState
  held: boolean
  /** SAFE | CAUTION | DO_NOT_INSTALL */
  recommendation?: string | null
  score?: number | null
}

/** GET /api/skills/{name}: the verdict in full, and the digest POST /{name}/approve takes back. */
export interface ApiSkillScanDetail extends ApiSkillScan {
  max_severity?: string | null
  findings: { rule_id: string; severity: string; message: string; file?: string | null }[]
  digest?: string | null
  error?: string | null
  approved_by?: string | null
  approved_at?: string | null
}

/** POST /api/skills/upload: the first skill installed, plus every skill the upload installed
 *  and the SKILL.md files a zip skipped, with a clause saying why. */
export interface ApiSkillUpload extends ApiSkill {
  skills: ApiSkill[]
  skipped: number
  skipped_note?: string | null
}

/** A file in the skill folder: posix path relative to it, and size in bytes. */
interface ApiSkillFile {
  path: string
  size: number
}

/** GET /api/skills/{name}: the Markdown body, without frontmatter, the folder's files and the version. */
export interface ApiSkillDetail extends ApiSkill {
  scan?: ApiSkillScanDetail
  body: string
  operator_root_set: boolean
  version: number
  files: ApiSkillFile[]
}

interface SkillWrite {
  name: string
  description: string
  body: string
  /** A loaded skill whose whole folder is copied under the new name. */
  source?: string
  /** The version the editor opened; an overwrite is refused (409) if it has moved. */
  version?: number
}

/** POST /api/skills/{name}/test: the saved skill's evals/cases.json run on the chat model. */
export interface ApiSkillTest {
  /** True when the skill ships no cases; nothing was run. */
  no_cases: boolean
  model: string | null
  results: { name: string; passed: boolean; missing: string[] }[]
}

/** A draft from POST /api/skills/generate: what the drawer's form holds. Nothing is saved until Save. */
export interface SkillDraft {
  name: string
  description: string
  body: string
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
  /** Installs a SKILL.md or a .zip of one or more skills under the operator root, all or none; a taken name is refused (409). */
  upload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api
      .post<ApiSkillUpload>('/skills/upload', form, { headers: { 'Content-Type': 'multipart/form-data' } })
      .then((r) => r.data)
  },
  generate: (description: string) =>
    api.post<{ draft: SkillDraft }>('/skills/generate', { description }, { timeout: LLM_TIMEOUT }).then((r) => r.data.draft),
  test: (name: string) =>
    api
      .post<ApiSkillTest>(`/skills/${encodeURIComponent(name)}/test`, null, { timeout: LLM_TIMEOUT })
      .then((r) => r.data),
  delete: (name: string) => api.delete(`/skills/${encodeURIComponent(name)}`).then((r) => r.data),
  /** Releases a held skill to agents. `digest` is the scanned content the approver read; refused (409) if the skill has changed since. */
  approve: (name: string, digest: string) =>
    api.post<ApiSkill>(`/skills/${encodeURIComponent(name)}/approve`, { digest }).then((r) => r.data),
}
