import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ColorSchemeProvider } from '../contexts/ColorSchemeContext'
import UserMenu from './UserMenu'

vi.mock('../services/api', () => ({
  configApi: {
    getTheme: () => Promise.resolve({ data: { theme: 'dark' } }),
    setTheme: () => Promise.resolve({ data: {} }),
  },
  consoleApi: {
    getHealth: () => Promise.resolve({ data: { version: '9.9.9', status: 'healthy' } }),
  },
}))

const auth = vi.hoisted(() => ({
  user: {
    username: 'dev-user',
    full_name: 'Test User' as string | null | undefined,
    email: 'dev@localhost',
    role_id: 'role-admin',
    mfa_enabled: false,
  },
  logout: vi.fn(),
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => auth,
}))

function renderMenu(path = '/cases', onShowTour = vi.fn(), setupLeft: number | null = null) {
  return render(
    <ColorSchemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/setup" element={<div>setup-route</div>} />
          <Route path="/:screen" element={<UserMenu onShowTour={onShowTour} setupLeft={setupLeft} />} />
        </Routes>
      </MemoryRouter>
    </ColorSchemeProvider>,
  )
}

describe('UserMenu', () => {
  beforeEach(() => {
    auth.user.username = 'dev-user'
    auth.user.full_name = 'Test User'
    auth.user.mfa_enabled = false
    auth.logout.mockReset().mockResolvedValue(undefined)
  })

  it('renders initials and labels from full_name', async () => {
    renderMenu()
    expect(screen.getByText('TU')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    expect(screen.getAllByText('Test User').length).toBeGreaterThan(0)
    expect(await screen.findByText('About Vigil · 9.9.9')).toBeInTheDocument()
    const feedback = screen.getByRole('menuitem', { name: /^Share feedback/ })
    const href = feedback.getAttribute('href') || ''
    const url = new URL(href)
    expect(url.origin + url.pathname).toBe('https://github.com/Vigil-SOC/vigil/issues/new')
    expect(url.searchParams.get('labels')).toBe('console-feedback')
    expect(url.searchParams.get('body')).toContain('Screen: cases')
    expect(url.searchParams.get('body')).toContain('Version: 9.9.9')
  })

  it('starts the console tour from the account menu', () => {
    const onShowTour = vi.fn()
    renderMenu('/cases', onShowTour)
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Take the tour' }))
    expect(onShowTour).toHaveBeenCalledOnce()
    expect(screen.queryByRole('menu', { name: 'Account' })).not.toBeInTheDocument()
  })

  it('opens setup from the account menu', () => {
    renderMenu()
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Setup guide' }))
    expect(screen.getByText('setup-route')).toBeInTheDocument()
  })

  it('lays the menu out as the board does, with one theme toggle', async () => {
    renderMenu()
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    const menu = await screen.findByRole('menu', { name: 'Account' })
    const rows = screen.getAllByRole('menuitem').map((row) => row.textContent?.trim())
    expect(rows).toEqual([
      'Setup guide',
      'Take the tour',
      expect.stringMatching(/^Share feedbackAbout Vigil · 9\.9\.9$/),
      'Switch to light mode',
      'Settings',
      'Sign out',
    ])
    expect(menu).toHaveTextContent('admin')
    expect(menu).not.toHaveTextContent('dev@localhost')
    expect(menu).not.toHaveTextContent('Role:')

    fireEvent.click(screen.getByRole('menuitem', { name: 'Switch to light mode' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Switch to dark mode' }))
    expect(screen.getByRole('menuitem', { name: 'Switch to light mode' })).toBeInTheDocument()
  })

  it('folds MFA into the identity line', async () => {
    auth.user.mfa_enabled = true
    renderMenu()
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    expect(await screen.findByRole('menu', { name: 'Account' })).toHaveTextContent('admin · MFA enabled')
  })

  it('shows the setup count from the shell and none at 0 or unread', () => {
    const { unmount } = renderMenu('/cases', vi.fn(), 2)
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    expect(screen.getByRole('menuitem', { name: 'Setup guide 2 left' })).toBeInTheDocument()
    unmount()

    for (const left of [0, null]) {
      const view = renderMenu('/cases', vi.fn(), left)
      fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
      expect(screen.getByRole('menuitem', { name: 'Setup guide' })).toBeInTheDocument()
      view.unmount()
    }
  })

  it('signs out and returns to the login page', async () => {
    renderMenu()
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Sign out' }))
    await waitFor(() => expect(auth.logout).toHaveBeenCalledOnce())
    expect(screen.queryByRole('menu', { name: 'Account' })).not.toBeInTheDocument()
  })

  it.each([null, undefined, '', '   '])(
    'falls back to username when full_name is %j',
    async (full_name) => {
      auth.user.full_name = full_name
      renderMenu()
      expect(screen.getByRole('button', { name: 'Account menu' })).toBeInTheDocument()
      expect(screen.getByText('D')).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
      expect(screen.getAllByText('dev-user').length).toBeGreaterThan(0)
      const menu = await screen.findByRole('menu', { name: 'Account' })
      expect(menu).toHaveTextContent('dev-user')
    },
  )
})
