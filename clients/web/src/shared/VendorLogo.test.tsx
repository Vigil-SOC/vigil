import { render, screen } from '@testing-library/react'
import VendorLogo, { vendorMark } from './VendorLogo'

describe('VendorLogo', () => {
  it('draws a Simple Icons mark, a lettered badge, or nothing', () => {
    expect(vendorMark('Palo Alto NGFW')?.kind).toBe('svg')
    expect(vendorMark('crowdstrike')).toMatchObject({ kind: 'letters', text: 'CS' })
    expect(vendorMark('Microsoft Entra ID')).toMatchObject({ kind: 'letters', text: 'EN' })
    expect(vendorMark('SentinelOne')).toMatchObject({ text: 'S1' })
    expect(vendorMark('DNS')).toBeNull()
    expect(vendorMark(null)).toBeNull()
  })

  it('names the vendor for screen readers', () => {
    render(<><VendorLogo name="Splunk" /><VendorLogo name="Wazuh" /><VendorLogo name="DNS" /></>)
    expect(screen.getByRole('img', { name: 'Splunk' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Wazuh' })).toHaveTextContent('W')
    expect(screen.getAllByRole('img')).toHaveLength(2)
  })
})

