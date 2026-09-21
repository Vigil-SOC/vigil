import axios from 'axios'
import { basePath } from '../config/basePath'

/**
 * Read-only client for the Skills API.
 *
 * Skills are files loaded from the repository or a mounted directory; the
 * console lists them and nothing more (epic #882, decision 7).
 */

/** Wire row from GET /api/skills: a skill loaded from disk (#928). */
export interface ApiSkill {
  name: string
  description: string
  source_path: string
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
  list: () => client.get<ApiSkill[]>('').then((r) => r.data),
}
