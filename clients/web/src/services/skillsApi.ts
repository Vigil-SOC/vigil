import axios from 'axios'
import { basePath } from '../config/basePath'

/**
 * Read-only client for the Skills API.
 *
 * Skills are files loaded from the repository or a mounted directory; the
 * console lists them and nothing more (epic #882, decision 7).
 */

/** Tolerates both the current DB row and the future file-loader shape. */
export interface Skill {
  skill_id?: string
  name: string
  description?: string | null
  source_path?: string | null
  category?: string
  version?: number
  is_active?: boolean
}

const client = axios.create({
  baseURL: `${basePath}/api/skills`,
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
  list: () => client.get<Skill[]>('').then((r) => r.data),
}
