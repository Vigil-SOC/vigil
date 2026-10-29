import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'
import SourceChip from './SourceChip'

vi.mock('../extensions/ExtensionProvider', () => ({
  useExtensions: () => ({
    extensions: [
      {
        integrationId: 'acme',
        connectorUrl: 'https://acme.example',
        manifest: { id: 'acme', name: 'Acme', badge: { label: 'Acme', logo: 'https://acme.example/logo.svg' } },
      },
    ],
  }),
}))

describe('SourceChip', () => {
  it('shows the logo a connector manifest badge carries', () => {
    const { container } = render(<SourceChip source="acme" />)
    expect(container.querySelector('img')).toHaveAttribute('src', 'https://acme.example/logo.svg')
    expect(container).toHaveTextContent('Acme')
  })

  it('keeps the icon for a source without a logo', () => {
    const { container } = render(<SourceChip source="splunk" />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('svg')).not.toBeNull()
  })
})
