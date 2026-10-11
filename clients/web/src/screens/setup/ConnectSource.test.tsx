import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import ConnectSource from './ConnectSource'
import type { IntegrationMetadata } from '../../config/integrationSchema'

const integration = (over: Partial<IntegrationMetadata> = {}): IntegrationMetadata => ({
  id: 'demo-src',
  name: 'Demo Source',
  category: 'SIEM',
  description: '',
  functionality_type: 'SIEM',
  fields: [{ name: 'url', label: 'URL', type: 'url', required: true }],
  ...over,
})

const renderCard = (i: IntegrationMetadata) =>
  render(
    <ConnectSource
      integration={i}
      existingConfig={{}}
      secretsSet={{}}
      onTest={vi.fn(() => Promise.resolve({ connected: true }))}
      onAdvance={vi.fn()}
      onAnother={vi.fn()}
    />,
  )

describe('ConnectSource', () => {
  it('hides "Where do I find this?" when the integration has no docs link', () => {
    renderCard(integration())
    expect(screen.getByText('Connect Demo Source')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Where do I find this/ })).not.toBeInTheDocument()
  })

  it('shows it, opening the docs, when there is one', () => {
    renderCard(integration({ docs_url: 'https://example.test/docs' }))
    expect(screen.getByRole('link', { name: /Where do I find this/ })).toHaveAttribute(
      'href',
      'https://example.test/docs',
    )
  })

  it('shows a toggle field as a switch, not a text box', () => {
    renderCard(
      integration({ fields: [{ name: 'verify', label: 'Verify TLS', type: 'boolean', default: true }] }),
    )
    fireEvent.click(screen.getByRole('switch'))
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
  })
})
