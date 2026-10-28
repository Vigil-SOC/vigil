import { fireEvent, render, screen } from '@testing-library/react'
import { findingsApi } from '../services/api'
import FindingDetails from './FindingDetails'

vi.mock('../services/api', () => ({
  findingsApi: { getById: vi.fn(), getEnrichment: vi.fn() },
}))

const finding = {
  finding_id: 'f-1',
  entity_context: { host: 'FIN-WS10', users: ['f.ortiz'], source_evidence: { raw: 'x' }, net: { src_ip: '10.0.30.40' } },
  mitre_predictions: { T1566: 0.4, T1204: null, 'T1059.001': 0.82 },
  ai_enrichment: null,
}

beforeEach(() => {
  vi.mocked(findingsApi.getById).mockResolvedValue({ data: finding } as never)
})

describe('FindingDetails', () => {
  it('shows the techniques and entities read from the finding', async () => {
    render(<FindingDetails findingId="f-1" />)
    const links = await screen.findAllByRole('link')
    expect(links.map((a) => a.textContent)).toEqual([
      expect.stringMatching(/^T1059\.001 .*82%$/),
      expect.stringMatching(/^T1566 .*40%$/),
      expect.stringMatching(/^T1204 .*confidence not given$/),
    ])
    expect(links[0]).toHaveAttribute('href', 'https://attack.mitre.org/techniques/T1059/001/')
    expect(screen.getByText('FIN-WS10')).toBeInTheDocument()
    expect(screen.getByText('Net · Src ip')).toBeInTheDocument()
    // the raw record has its own panel
    expect(screen.queryByText('Source evidence')).toBeNull()
  })

  it('offers an AI summary when there is none, and shows it once written', async () => {
    vi.mocked(findingsApi.getEnrichment).mockResolvedValue({
      data: { enrichment: { threat_summary: 'Office spawned encoded PowerShell.', risk_level: 'high', recommended_actions: ['Isolate FIN-WS10'] } },
    } as never)
    render(<FindingDetails findingId="f-1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Summarise with AI' }))
    expect(await screen.findByText('Office spawned encoded PowerShell.')).toBeInTheDocument()
    expect(screen.getByText('Risk: high')).toBeInTheDocument()
    expect(screen.getByText('Isolate FIN-WS10')).toBeInTheDocument()
  })

  it('does not put a late summary or error on the next alert', async () => {
    let fail: (e: Error) => void = () => {}
    vi.mocked(findingsApi.getEnrichment).mockReturnValue(new Promise((_, reject) => { fail = reject }) as never)
    vi.mocked(findingsApi.getById).mockImplementation(async (id: string) => ({ data: { ...finding, finding_id: id } }) as never)
    const { rerender } = render(<FindingDetails findingId="f-1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Summarise with AI' }))
    rerender(<FindingDetails findingId="f-2" />)
    const button = await screen.findByRole('button', { name: 'Summarise with AI' })
    fail(new Error('timeout'))
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByText('The AI summary could not be generated right now.')).toBeNull()
    expect(button).not.toBeDisabled()
  })

  it('renders nothing when the finding cannot be read', async () => {
    vi.mocked(findingsApi.getById).mockRejectedValue(new Error('404'))
    const { container } = render(<FindingDetails findingId="f-1" />)
    await new Promise((r) => setTimeout(r, 0))
    expect(container).toBeEmptyDOMElement()
  })
})
