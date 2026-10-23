import { beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import IntegrationsSection from './IntegrationsSection'
import { tabFromQuery } from './integrationsData'
import { IntegrationsStateProvider } from './IntegrationsState'
import { configApi, detectionRulesApi, mcpApi } from '../../services/api'

import { getAllIntegrations } from '../../config/integrations'
import { configApi } from '../../services/api'
vi.mock('../../services/api', () => ({
  default: {}, aiConfigApi: {}, budgetsApi: {}, federationApi: {}, ingestionApi: {}, kafkaApi: {}, localServicesApi: {}, orchestratorApi: {}, storageApi: {},
  configApi: { getIntegrations: vi.fn(() => Promise.resolve({ data: {} })), setIntegrations: vi.fn() },
  detectionRulesApi: { listSources: vi.fn(() => Promise.resolve({ data: { sources: [] } })), getStats: vi.fn(() => Promise.resolve({ data: {} })), updateAll: vi.fn() },
  mcpApi: { listServers: vi.fn(() => Promise.resolve({ data: { servers: [] } })), getStatuses: vi.fn(() => Promise.resolve({ data: { statuses: [] } })), setServerEnabled: vi.fn(), reloadServers: vi.fn() },
}))
let listed = 1, status = 200, lists = 0
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/list')) { lists++; return { ok: status === 200, json: async () => (status === 200 ? { success: true, integrations: Array.from({ length: listed }, (_, i) => ({ id: `c${i}`, name: `Cust ${i}` })) } : { detail: 'forbidden' }) } }
    if (url.endsWith('/generate')) return { ok: true, json: async () => ({ success: true, integration_id: 'custom-acme', integration_name: 'Acme', metadata: { category: 'EDR/XDR', description: 'x', fields: [] }, tools: [{ name: 'acme_list', description: 'd' }], server_code: 'print(1)' }) }
    if (url.endsWith('/validate')) return { ok: true, json: async () => ({ valid: true, checks: { has_server: true, has_main: true } }) }
    if (url.endsWith('/save')) { listed++; return { ok: true, json: async () => ({ success: true }) } }
    return { ok: true, json: async () => ({}) }
  }))
})
const mount = () => render(<MemoryRouter initialEntries={['/settings?section=integrations&tab=custom']}><IntegrationsStateProvider><IntegrationsSection notify={vi.fn()} /></IntegrationsStateProvider></MemoryRouter>)
const custom = () => getAllIntegrations().filter((i) => i.is_custom).map((i) => i.id).join(',')
it('probe', async () => {
  mount()
  await screen.findByRole('tab', { name: 'Custom 1' })
  await new Promise((r) => setTimeout(r, 200))
  process.stderr.write(`list GETs on one mount: ${lists}; catalog customs: [${custom()}]` + "\n")
  fireEvent.change(screen.getByLabelText(/^API documentation/), { target: { value: 'GET /v2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Generate draft' }))
  await screen.findByText('acme_list')
  fireEvent.click(screen.getByRole('button', { name: 'Validate' }))
  await screen.findByRole('tab', { name: 'Custom 2' })
  process.stderr.write(`after Validate: GETs ${lists}; tab "Custom 2"; catalog customs: [${custom()}]; config GETs ${vi.mocked(configApi.getIntegrations).mock.calls.length}` + "\n")
  expect(custom()).toBe('c0,c1')
  expect(vi.mocked(configApi.getIntegrations).mock.calls.length).toBe(1)
  cleanup(); status = 403; lists = 0
  mount()
  await waitFor(() => expect(lists).toBe(1))
  await new Promise((r) => setTimeout(r, 200))
  process.stderr.write(`403 mount: GETs ${lists}; tab label "${screen.getByRole('tab', { name: /^Custom/ }).textContent}"; catalog customs: [${custom()}]` + "\n")
  expect(screen.getByRole('tab', { name: 'Custom' })).toBeInTheDocument()
  expect(custom()).toBe('')
})
