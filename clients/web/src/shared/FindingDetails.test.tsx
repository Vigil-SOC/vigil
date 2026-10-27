import { fireEvent, render, screen } from '@testing-library/react'
import { findingsApi } from '../services/api'
import FindingDetails from './FindingDetails'

vi.mock('../services/api', () => ({
  findingsApi: { getById: vi.fn(), similar: vi.fn(), getEnrichment: vi.fn() },
}))

const finding = {
  finding_id: 'f-1',
  entity_context: { host: 'FIN-WS10', users: ['f.ortiz'], source_evidence: { raw: 'x' }, net: { src_ip: '10.0.30.40' } },
  mitre_predictions: { 'T1059.001': 0.82, T1566: 0.4 },
  ai_enrichment: null,
}

beforeEach(() => {
  vi.mocked(findingsApi.getById).mockResolvedValue({ data: finding } as never)
  vi.mocked(findingsApi.similar).mockResolvedValue({
    data: { finding_id: 'f-1', compared: 3, method: 'word overlap', similar: [{ finding_id: 'f-2', title: 'Beacon to new domain', score: 0.6, reasons: ['same FIN-WS10'] }] },
  } as never)
})

describe('FindingDetails', () => {
  it('shows the techniques, entities and look-alike alerts read from the finding', async () => {
    render(<FindingDetails findingId="f-1" />)
    expect(await screen.findByText('Beacon to new domain')).toBeInTheDocument()
    expect(screen.getByText('same FIN-WS10')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /T1059\.001 · 82%/ })).toHaveAttribute('href', 'https://attack.mitre.org/techniques/T1059/001/')
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

  it('renders nothing when the finding cannot be read', async () => {
    vi.mocked(findingsApi.getById).mockRejectedValue(new Error('404'))
    const { container } = render(<FindingDetails findingId="f-1" />)
    await new Promise((r) => setTimeout(r, 0))
    expect(container).toBeEmptyDOMElement()
  })
})
