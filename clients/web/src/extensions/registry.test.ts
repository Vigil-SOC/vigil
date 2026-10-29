import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchManifest } from './registry'

const manifest = (logo: string) => ({
  id: 'acme',
  name: 'Acme',
  version: '1.0.0',
  hostApiVersion: '1.0',
  badge: { label: 'Acme', logo },
  render: { mode: 'element', bundleUrl: 'bundle.js', elementTag: 'acme-page' },
  mountPoints: [{ type: 'screen', key: 'acme', navLabel: 'Acme', title: 'Acme' }],
})

const serve = (body: unknown) =>
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: async () => body })))

describe('fetchManifest badge logo', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('resolves a relative logo against the connector base', async () => {
    serve(manifest('static/logo.svg'))
    const ext = await fetchManifest('acme', 'https://acme.example/connector')
    expect(ext?.manifest.badge?.logo).toBe('https://acme.example/connector/static/logo.svg')
  })

  it('drops a logo from another origin but keeps the connector', async () => {
    serve(manifest('https://elsewhere.example/logo.svg'))
    const ext = await fetchManifest('acme', 'https://acme.example')
    expect(ext).not.toBeNull()
    expect(ext?.manifest.badge?.logo).toBeUndefined()
  })
})
