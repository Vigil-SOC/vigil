/* The catalogue's price rows come from the /models/details row, not /models/parameters:
   at the pinned gateway the latter carries only capability flags, so reading rates
   from it left every price blank. */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import AiModelsPanel from './AiModelsPanel'

const modelDetails = vi.fn()
const modelParameters = vi.fn()

vi.mock('../../services/bifrostApi', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  bifrostApi: {
    modelDetails: (...a: unknown[]) => modelDetails(...(a as [])),
    modelParameters: (...a: unknown[]) => modelParameters(...(a as [])),
  },
}))

const MODELS = [
  {
    name: 'claude-opus-4-7',
    provider: 'anthropic',
    input_cost_per_token: 0.000005,
    output_cost_per_token: 0.000025,
    cache_read_input_token_cost: 0.0000005,
    cache_creation_input_token_cost: 0.00000625,
  },
  { name: 'mystery-model', provider: 'custom' },
  { name: 'llama3', provider: 'ollama', input_cost_per_token: 0, output_cost_per_token: 0 },
  {
    name: 'gpt-override',
    provider: 'openai',
    input_cost_per_token: 0.000005,
    output_cost_per_token: 0.000025,
    overridden_pricing: { input_cost_per_token: 0.000007 },
  },
]

const open = async (name: string) => {
  modelDetails.mockResolvedValue({ data: { models: MODELS, total: MODELS.length } })
  // Parameters never resolve: the rates must not wait on them.
  modelParameters.mockReturnValue(new Promise(() => {}))
  render(<AiModelsPanel />)
  fireEvent.click(await screen.findByText(name))
  const panel = screen.getByTitle('Close').closest('div')!.parentElement!
  return (label: string) => within(panel).getByText(label).nextElementSibling!.textContent
}

describe('AiModelsPanel price rows', () => {
  it('renders all four rates from the details row', async () => {
    const row = await open('claude-opus-4-7')
    expect(row('Input')).toBe('$5.00')
    expect(row('Output')).toBe('$25.00')
    expect(row('Cache read')).toBe('$0.500')
    expect(row('Cache write')).toBe('$6.25')
  })

  it('shows "not priced" when a base rate is missing, and a dash for absent cache rates', async () => {
    const row = await open('mystery-model')
    expect(row('Input')).toBe('not priced')
    expect(row('Output')).toBe('not priced')
    expect(row('Cache read')).toBe('—')
  })

  it('treats an explicit zero as a rate', async () => {
    const row = await open('llama3')
    expect(row('Input')).toBe('$0.000')
    expect(row('Output')).toBe('$0.000')
  })

  it('shows a gateway pricing override patched over the catalogue rate', async () => {
    const row = await open('gpt-override')
    expect(row('Input')).toBe('$7.00')
    expect(row('Output')).toBe('$25.00')
  })
})
