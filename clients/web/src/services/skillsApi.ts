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
  /** Set for a skill installed from a git repo: the repo, the commit, and how many skills came with it. */
  source?: ApiSkillSource | null
  /** Set on every row while the operator root is under the declared directory (VIGIL_INTENT_DIR): Vigil writes none of it. */
  managed?: ApiSkillsManaged | null
}

export interface ApiSkillsManaged {
  directory: string
  /** First line of the directory's REVISION marker; "unknown" without one. */
  revision: string
}

/** Why every save, import, update and delete is off while skills are managed (#5070). */
export function skillsManagedText(m: ApiSkillsManaged): string {
  return `Skills are managed by ${m.directory} at revision ${m.revision}. Change them by committing there; Vigil does not write them.`
}

export interface ApiSkillSource {
  label: string
  ref?: string | null
  path: string
  commit: string
  skills: number
}

/** One skill an update would change, add or remove. `edited`: changed in Vigil since it was installed. */
export interface ApiSkillLibraryChange {
  name: string
  folder: string
  note: string
  edited: boolean
  problem?: string | null
}

/** POST /api/skills/{name}/library/preview: what updating the skill's library would do, read at `latest`. */
export interface ApiSkillLibraryPreview {
  label: string
  ref?: string | null
  path: string
  commit: string
  latest: string
  changed: ApiSkillLibraryChange[]
  new: ApiSkillLibraryChange[]
  gone: ApiSkillLibraryChange[]
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

/** POST /api/skills/repo/preview: the skills a git repo holds, read at `commit`. */
export interface ApiSkillRepoPreview {
  label: string
  ref?: string | null
  path: string
  commit: string
  /** `installed`: the only problem is that the name is taken; the skill can still be scanned. */
  skills: { folder: string; name: string; description: string; problem?: string | null; installed?: boolean }[]
}

/** POST /api/skills/repo/scan: each picked skill's verdict, and the Finding each held one became. */
export interface ApiSkillRepoScanned {
  name: string
  folder: string
  /** scanned | failed | unavailable | disabled | not_scanned; only scanned carries a verdict. */
  status: string
  held?: boolean | null
  recommendation?: string | null
  score?: number | null
  max_severity?: string | null
  reasons: { rule_id: string; severity: string; message: string; file?: string | null }[]
  error?: string | null
  finding_id?: string | null
  /** False with a finding_id: an earlier scan of the same content raised it. */
  finding_new: boolean
}

export interface ApiSkillRepoScan {
  label: string
  commit: string
  /** "static" or "llm:<model>" */
  scan_mode: string
  skills: ApiSkillRepoScanned[]
}

/** The server clones the repo for each call; a large one takes a while. */
const REPO_TIMEOUT = 150_000

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
  /** Reads a git repo (owner/repo, a GitHub link or an https git URL; @ref for a branch or tag) and lists its skills. Installs nothing. */
  previewRepo: (source: string) =>
    api.post<ApiSkillRepoPreview>('/skills/repo/preview', { source }, { timeout: REPO_TIMEOUT }).then((r) => r.data),
  /** Installs the picked skills from the repo at the commit the preview read; refused (409) if the repo has moved or a name is taken. */
  installRepo: (source: string, commit: string, names: string[]) =>
    api.post<ApiSkillUpload>('/skills/repo/install', { source, commit, names }, { timeout: REPO_TIMEOUT }).then((r) => r.data),
  /** Scans the picked skills at the commit the preview read, installing nothing; held ones become Findings. 409 if the repo has moved. */
  scanRepo: (source: string, commit: string, names: string[]) =>
    api.post<ApiSkillRepoScan>('/skills/repo/scan', { source, commit, names }, { timeout: REPO_TIMEOUT }).then((r) => r.data),
  /** The commit the skill's repo is at now (no clone); null when its branch or tag is gone. */
  latest: (name: string) =>
    api.get<{ commit: string | null }>(`/skills/${encodeURIComponent(name)}/library/latest`).then((r) => r.data.commit),
  libraryPreview: (name: string) =>
    api.post<ApiSkillLibraryPreview>(`/skills/${encodeURIComponent(name)}/library/preview`, null, { timeout: REPO_TIMEOUT }).then((r) => r.data),
  /** Takes the picked changes at the commit the preview read; refused (409) if the repo has moved. */
  libraryUpdate: (name: string, commit: string, take: string[], edited: string[]) =>
    api
      .post<{ skills: ApiSkill[]; removed: string[] }>(`/skills/${encodeURIComponent(name)}/library/update`, { commit, take, edited }, { timeout: REPO_TIMEOUT })
      .then((r) => r.data),
  /** Deletes every skill installed with this one from its repo. */
  deleteLibrary: (name: string) =>
    api.delete<{ deleted: string[] }>(`/skills/${encodeURIComponent(name)}/library`).then((r) => r.data),
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
