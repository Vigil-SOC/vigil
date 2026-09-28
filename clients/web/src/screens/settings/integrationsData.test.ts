import { describe, it, expect } from 'vitest'
import {
  getIntegrationForServer,
  MCP_CATEGORIES,
  SERVER_TO_INTEGRATION,
} from './integrationsData'

// Both Splunk servers share the descriptor id `splunk`. The self-hosted one is
// the card the Splunk REST form configures (#1113); the official one is
// configured by SPLUNK_MCP_URL and must not open that form, or an operator who
// thinks they pointed Vigil at the official MCP server has written a port-8089
// REST config instead.
describe('getIntegrationForServer', () => {
  it('opens the Splunk REST form from the self-hosted card', () => {
    const integration = getIntegrationForServer('splunk-selfhosted')
    expect(integration?.id).toBe('splunk')
    expect(integration?.fields?.length).toBeGreaterThan(0)
  })

  it('gives the official splunk card no form, even though a catalog id matches its name', () => {
    expect(getIntegrationForServer('splunk')).toBeUndefined()
  })

  it('resolves every aliased server to the integration it maps to', () => {
    for (const [server, id] of SERVER_TO_INTEGRATION) {
      expect(getIntegrationForServer(server)?.id, server).toBe(id)
    }
  })

  it('shows the self-hosted server as a Settings card', () => {
    const siem = MCP_CATEGORIES.find((c) => c.servers.includes('splunk-selfhosted'))
    expect(siem?.servers).toContain('splunk')
    expect(siem?.servers).toContain('splunk-selfhosted')
  })
})
