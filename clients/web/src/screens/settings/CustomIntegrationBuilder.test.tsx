import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import api from '../../services/api'
import CustomIntegrationBuilder from './CustomIntegrationBuilder'

const draft = {
  success: true,
  integration_id: 'acme',
  integration_name: 'Acme',
  metadata: { fields: [] },
  tools: [],
  server_code: 'print(1)',
}

function serve(seen: { url?: string; csrf: unknown }[]) {
  return async (config: InternalAxiosRequestConfig): Promise<AxiosResponse> => {
    const csrf = config.headers['X-CSRF-Token']
    seen.push({ url: config.url, csrf })
    const reply = (status: number, data: unknown) => ({ data, status, statusText: '', headers: {}, config })
    if (csrf !== 'tok') {
      const res = reply(403, { detail: 'CSRF token missing or invalid' })
      throw new AxiosError('Request failed with status code 403', 'ERR_BAD_REQUEST', config, null, res)
    }
    if (config.url?.endsWith('/validate')) return reply(200, { valid: true, checks: { compiles: true } })
    if (config.url?.endsWith('/save')) return reply(200, { success: true })
    return reply(200, draft)
  }
}

function renderBuilder() {
  render(<CustomIntegrationBuilder notify={vi.fn()} onWrote={vi.fn()} onSaved={vi.fn()} />)
  fireEvent.change(screen.getByPlaceholderText(/Paste API documentation/), { target: { value: 'docs' } })
  fireEvent.click(screen.getByText('Generate draft'))
}

describe('CustomIntegrationBuilder', () => {
  const adapter = api.defaults.adapter

  beforeEach(() => {
    document.cookie = 'csrf_token=tok'
  })

  afterEach(() => {
    api.defaults.adapter = adapter
    document.cookie = 'csrf_token=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    cleanup()
  })

  it('sends the CSRF header on generate, save and validate', async () => {
    const seen: { url?: string; csrf: unknown }[] = []
    api.defaults.adapter = serve(seen)
    renderBuilder()
    await screen.findByText('Acme')
    fireEvent.click(screen.getByText('Validate'))
    await screen.findByText('The code passes the static check.')
    expect(seen).toEqual([
      { url: '/custom-integrations/generate', csrf: 'tok' },
      { url: '/custom-integrations/save', csrf: 'tok' },
      { url: '/custom-integrations/acme/validate', csrf: 'tok' },
    ])
  })

  it('shows the detail the server refused with', async () => {
    document.cookie = 'csrf_token=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    api.defaults.adapter = serve([])
    renderBuilder()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('CSRF token missing or invalid'))
  })
})
