import { describe, expect, it } from 'vitest'
import type { LLMProvider } from '../../services/api'
import type { BifrostKey } from '../../services/bifrostApi'
import { bifrostStaysOnSite, legacyStaysOnSite, residencyCopy } from './providerResidency'

const legacy = (over: Partial<LLMProvider>): LLMProvider =>
  ({ provider_type: 'openai', name: 'hosted', base_url: null, ...over }) as LLMProvider

const key = (url: string): BifrostKey =>
  ({ id: 'k', name: 'k', models: ['*'], weight: 1, ollama_key_config: { url } })

describe('provider residency', () => {
  it('keeps Ollama and loopback on site, and says so', () => {
    expect(bifrostStaysOnSite('ollama', [])).toBe(true)
    expect(bifrostStaysOnSite('openai', [key('http://127.0.0.1:11434')])).toBe(true)
    expect(bifrostStaysOnSite('openai', [key('http://[::1]:11434')])).toBe(true)
    expect(legacyStaysOnSite(legacy({ provider_type: 'ollama' }))).toBe(true)
    expect(legacyStaysOnSite(legacy({ base_url: 'http://localhost:11434/v1' }))).toBe(true)
    expect(residencyCopy(true)).toBe('data stays on site')
  })

  it('treats any other provider as leaving the site', () => {
    expect(bifrostStaysOnSite('anthropic', [])).toBe(false)
    expect(bifrostStaysOnSite('gemini', [key('https://generativelanguage.googleapis.com')])).toBe(false)
    expect(legacyStaysOnSite(legacy({ provider_type: 'openai', base_url: 'https://api.openai.com' }))).toBe(false)
    expect(residencyCopy(false)).toBe('data leaves the site')
  })
})
