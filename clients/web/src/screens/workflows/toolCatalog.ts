// The tools an agent or a workflow step may be given, read once from
// GET /agents/custom/_meta/tools and shared by every picker on the page.
import { useEffect, useState } from 'react'
import { agentsApi } from '../../services/api'
import type { ToolChange } from '../../data/appData'

export interface CatalogTool {
  name: string
  /** "vigil" for Vigil's own tools, else the integration that serves it */
  server: string
  /** what the picker files it under: Findings, Cases… for Vigil's, the integration otherwise */
  group: string
  description: string
  changes: ToolChange
}

export interface ToolCatalog {
  phase: 'loading' | 'ready' | 'error'
  tools: CatalogTool[]
  byName: Map<string, CatalogTool>
}

// Integration keys as servers name themselves, said the way people say them.
const SERVER_LABEL: Record<string, string> = {
  vigil: 'Vigil',
  splunk: 'Splunk',
  crowdstrike: 'CrowdStrike',
  cs: 'CrowdStrike',
  okta: 'Okta',
  entra: 'Microsoft Entra ID',
  defender: 'Microsoft Defender',
  sentinel: 'Microsoft Sentinel',
  jira: 'Jira',
  slack: 'Slack',
  servicenow: 'ServiceNow',
  virustotal: 'VirusTotal',
  vt: 'VirusTotal',
  opencti: 'OpenCTI',
  cf: 'Cloudflare',
  cloudflare: 'Cloudflare',
  vstrike: 'vStrike',
  cape: 'CAPE sandbox',
  ha: 'Hybrid Analysis',
  anyrun: 'ANY.RUN',
  url: 'URL analysis',
  paloalto: 'Palo Alto NGFW',
  panos: 'Palo Alto NGFW',
  aws: 'AWS',
  elastic: 'Elastic',
  wazuh: 'Wazuh',
  github: 'GitHub',
  atomic: 'Atomic Red Team',
  loglm: 'DeepTempo LogLM',
  deeptempo: 'DeepTempo LogLM',
}

export function serverLabel(server: string): string {
  const key = server.toLowerCase()
  if (SERVER_LABEL[key]) return SERVER_LABEL[key]
  return key.split(/[-_]/).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
}

/** A tool's name without its server prefix, for reading: "splunk_search" → "search". */
export function shortName(t: Pick<CatalogTool, 'name' | 'server'>): string {
  const prefix = `${t.server}_`
  return t.server !== 'vigil' && t.name.startsWith(prefix) ? t.name.slice(prefix.length) : t.name
}

export const CHANGE_WORD: Record<string, string> = {
  read_only: 'Reads only',
  asks_first: 'Asks you first',
  on_its_own: 'Changes things',
}

interface ApiCatalogRow { name: string; server?: string; group?: string; description?: string; changes?: ToolChange }

/** Rows from the response; an older server without `catalog` gives names and marks only. */
export function catalogFrom(data: unknown): CatalogTool[] {
  const d = (data ?? {}) as { catalog?: ApiCatalogRow[]; changes?: Record<string, ToolChange> }
  if (Array.isArray(d.catalog)) {
    return d.catalog
      .filter((r) => r && typeof r.name === 'string')
      .map((r) => ({
        name: r.name,
        server: r.server || 'vigil',
        group: r.group || serverLabel(r.server || 'vigil'),
        description: r.description || '',
        changes: (r.changes ?? 'read_only') as ToolChange,
      }))
  }
  return Object.entries(d.changes ?? {}).map(([name, changes]) => ({ name, server: 'vigil', group: 'Tools', description: '', changes }))
}

let cached: Promise<CatalogTool[]> | null = null

/** Asked once per page load; a failed ask is asked again next time. */
function loadCatalog(): Promise<CatalogTool[]> {
  if (!cached) {
    cached = Promise.resolve()
      .then(() => agentsApi.getAvailableTools())
      .then((r) => catalogFrom(r?.data))
      .catch((e) => { cached = null; throw e })
  }
  return cached
}

/** For tests, and after an integration connects. */
export function forgetToolCatalog() { cached = null }

export function useToolCatalog(): ToolCatalog {
  const [state, setState] = useState<ToolCatalog>({ phase: 'loading', tools: [], byName: new Map() })
  useEffect(() => {
    let cancelled = false
    loadCatalog()
      .then((tools) => { if (!cancelled) setState({ phase: 'ready', tools, byName: new Map(tools.map((t) => [t.name, t])) }) })
      .catch(() => { if (!cancelled) setState({ phase: 'error', tools: [], byName: new Map() }) })
    return () => { cancelled = true }
  }, [])
  return state
}
