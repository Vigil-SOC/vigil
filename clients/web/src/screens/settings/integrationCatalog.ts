import type { IntegrationMetadata } from '../../config/integrationSchema'
import {
  MCP_CATEGORIES,
  SERVER_DESCRIPTIONS,
  SERVER_DISPLAY_NAMES,
  SERVER_TO_INTEGRATION,
  WIP_SERVERS,
  prettyServerName,
} from './integrationsData'
import type { ServerRow } from './integrationHealth'

/** One card of Add integration. `integration` is set when Connect opens the
 *  setup drawer; a server with no integration descriptor is only switched on. */
export interface CatalogEntry {
  key: string
  server: string
  name: string
  category: string
  description: string
  connected: boolean
  integration?: IntegrationMetadata
}

export const OTHER_CATEGORY = 'Other'

const INTEGRATION_TO_SERVER = new Map([...SERVER_TO_INTEGRATION].map(([server, id]) => [id, server]))
const categoryOf = (server: string) => MCP_CATEGORIES.find((c) => c.servers.includes(server))?.label ?? OTHER_CATEGORY

/** What the console really offers: the client catalog plus servers that have no
 *  descriptor, minus work-in-progress servers, in category order. */
export function buildCatalog(catalog: IntegrationMetadata[], rows: ServerRow[], connectedIds: Set<string>): CatalogEntry[] {
  const fromCatalog = catalog.map((i): CatalogEntry => {
    const server = INTEGRATION_TO_SERVER.get(i.id) ?? i.id
    return {
      key: `i:${i.id}`,
      server,
      name: SERVER_DISPLAY_NAMES.get(server) ?? i.name,
      category: categoryOf(server),
      description: SERVER_DESCRIPTIONS.get(server) ?? i.description,
      connected: connectedIds.has(i.id) || !!rows.find((r) => r.name === server)?.connected,
      integration: i.fields?.length ? i : undefined,
    }
  })
  const described = new Set(fromCatalog.map((e) => e.server))
  const serverOnly = rows
    .filter((r) => !r.integration && !described.has(r.name))
    .map((r): CatalogEntry => ({
      key: `s:${r.name}`,
      server: r.name,
      name: SERVER_DISPLAY_NAMES.get(r.name) ?? prettyServerName(r.name),
      category: categoryOf(r.name),
      description: SERVER_DESCRIPTIONS.get(r.name) ?? 'Custom MCP integration.',
      connected: r.connected,
    }))
  const rank = (e: CatalogEntry) => {
    const i = MCP_CATEGORIES.findIndex((c) => c.label === e.category)
    return i < 0 ? MCP_CATEGORIES.length : i
  }
  return [...fromCatalog, ...serverOnly]
    .filter((e) => !WIP_SERVERS.has(e.server))
    // an entry with no descriptor fields is only offered if its server exists to switch on
    .filter((e) => e.integration || rows.some((r) => r.name === e.server))
    .sort((a, b) => rank(a) - rank(b))
}

/** Chips: every category that has a card, in display order. */
export function categoryLabels(entries: CatalogEntry[]): string[] {
  const labels = [...MCP_CATEGORIES.map((c) => c.label), OTHER_CATEGORY]
  return labels.filter((l) => entries.some((e) => e.category === l))
}
