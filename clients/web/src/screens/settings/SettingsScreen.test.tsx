import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '../../shell/toast'
import type { ConsoleScreenProps } from '../../shared/types'
import SettingsScreen from './SettingsScreen'

vi.mock('./AiConfigSection', () => ({ default: () => <div>AI panel</div> }))
vi.mock('./IntegrationsSection', () => ({ default: () => <div>Integrations panel</div> }))
vi.mock('./FederationSection', () => ({ default: () => <div>Federation panel</div> }))
vi.mock('./SlaPoliciesSection', () => ({ default: () => <div>SLA panel</div> }))
vi.mock('./AutoInvestigateSection', () => ({ default: () => <div>Autonomy panel</div> }))
vi.mock('./ServicesSection', () => ({ default: () => <div>Services panel</div> }))
vi.mock('./SystemSection', () => ({ default: () => <div>System panel</div> }))
vi.mock('./GeneralSection', () => ({ default: () => <div>General panel</div> }))
vi.mock('./DeveloperSection', () => ({ default: () => <div>Developer panel</div> }))
vi.mock('./UsersSection', () => ({ default: () => <div>Users panel</div> }))
vi.mock('./DataIngestion', () => ({ default: () => <div>Ingestion panel</div> }))
vi.mock('./DetectionRulesPanel', () => ({ default: () => <div>Detection panel</div> }))

const screenProps: ConsoleScreenProps = {
  openChat: vi.fn(),
  go: vi.fn(),
  goSettings: vi.fn(),
  setViewFull: vi.fn(),
}

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <SettingsScreen {...screenProps} />
      </ToastProvider>
    </MemoryRouter>,
  )
}

function nav() {
  const node = document.querySelector('.settings-nav')
  if (!node) throw new Error('settings nav missing')
  return within(node as HTMLElement)
}

function tabs() {
  const node = document.querySelector('.tabs')
  if (!node) throw new Error('tabs missing')
  return within(node as HTMLElement)
}

describe('settings nav', () => {
  it('groups the existing panels under the seven screens', () => {
    renderAt('/settings')
    const labels = ['AI models', 'Integrations', 'Alert collection', 'SLA policies', 'Limits & autonomy', 'Data & uploads', 'System']
    for (const label of labels) {
      expect(nav().getByRole('button', { name: label })).toBeInTheDocument()
    }
    expect(nav().queryByRole('button', { name: 'Services' })).not.toBeInTheDocument()
    expect(nav().queryByRole('button', { name: 'Users' })).not.toBeInTheDocument()
    expect(nav().queryByRole('button', { name: 'Developer' })).not.toBeInTheDocument()
    expect(screen.getByText('AI panel')).toBeInTheDocument()
  })

  it('heads the nav and every section with a title and a one-line description', () => {
    renderAt('/settings')
    expect(screen.getByRole('navigation', { name: 'Settings sections' })).toHaveTextContent('Settings')
    for (const label of ['AI models', 'Integrations', 'Alert collection', 'SLA policies', 'Limits & autonomy', 'Data & uploads', 'System']) {
      fireEvent.click(nav().getByRole('button', { name: label }))
      const head = document.querySelector('.page-head') as HTMLElement
      expect(within(head).getByRole('heading', { name: label })).toBeInTheDocument()
      expect(head.querySelector('p')?.textContent).toBeTruthy()
      expect(nav().getByRole('button', { name: label })).toHaveAttribute('aria-current', 'page')
    }
    // the tab strip sits under the System head
    expect(tabs().getByRole('button', { name: 'Services' })).toBeInTheDocument()
  })

  it('opens System on the bookmarked panel', () => {
    const { unmount } = render(
      <MemoryRouter initialEntries={['/settings?section=users']}>
        <ToastProvider>
          <SettingsScreen {...screenProps} />
        </ToastProvider>
      </MemoryRouter>,
    )
    expect(nav().getByRole('button', { name: 'System' })).toHaveClass('active')
    expect(tabs().getByRole('button', { name: 'Users' })).toHaveClass('active')
    expect(screen.getByText('Users panel')).toBeInTheDocument()
    expect(screen.queryByText('Developer panel')).not.toBeInTheDocument()
    expect(tabs().queryByRole('button', { name: 'Developer' })).not.toBeInTheDocument()

    unmount()
    renderAt('/settings?section=system')
    expect(tabs().getByRole('button', { name: 'System' })).toHaveClass('active')
    expect(screen.getByText('System panel')).toBeInTheDocument()

    fireEvent.click(tabs().getByRole('button', { name: 'Services' }))
    expect(screen.getByText('Services panel')).toBeInTheDocument()

    fireEvent.click(nav().getByRole('button', { name: 'AI models' }))
    expect(screen.getByText('AI panel')).toBeInTheDocument()
  })

  it('sends a dev-gated or unknown section back to the first section', () => {
    const { unmount } = render(
      <MemoryRouter initialEntries={['/settings?section=dev']}>
        <ToastProvider>
          <SettingsScreen {...screenProps} />
        </ToastProvider>
      </MemoryRouter>,
    )
    expect(screen.getByText('AI panel')).toBeInTheDocument()
    expect(nav().getByRole('button', { name: 'AI models' })).toHaveClass('active')
    unmount()

    renderAt('/settings?section=nope')
    expect(screen.getByText('AI panel')).toBeInTheDocument()
    expect(screen.queryByText('System panel')).not.toBeInTheDocument()
  })

  it('puts upload and detection rules on Data & uploads, with the retention line', () => {
    renderAt('/settings?section=data&tab=detection')
    expect(screen.getByText('Retention: Not measured yet')).toBeInTheDocument()
    expect(tabs().getByRole('button', { name: 'Detection Rules' })).toHaveClass('active')
    expect(screen.getByText('Detection panel')).toBeInTheDocument()
    expect(screen.queryByText('Ingestion panel')).not.toBeInTheDocument()

    fireEvent.click(tabs().getByRole('button', { name: 'Manual Upload' }))
    expect(screen.getByText('Ingestion panel')).toBeInTheDocument()
  })

  it('keeps the sections goSettings already opens', () => {
    renderAt('/settings?section=ai-config')
    expect(screen.getByText('AI panel')).toBeInTheDocument()

    fireEvent.click(nav().getByRole('button', { name: 'Integrations' }))
    expect(screen.getByText('Integrations panel')).toBeInTheDocument()

    fireEvent.click(nav().getByRole('button', { name: 'Alert collection' }))
    expect(screen.getByText('Federation panel')).toBeInTheDocument()

    fireEvent.click(nav().getByRole('button', { name: 'Limits & autonomy' }))
    expect(screen.getByText('Autonomy panel')).toBeInTheDocument()
  })
})
