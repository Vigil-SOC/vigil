import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ColorSchemeProvider } from '../contexts/ColorSchemeContext'
import UserMenu from './UserMenu'
import { SocThemeProvider } from './theme'

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

function renderMenu(path = '/cases', onShowTour = vi.fn()) {
  return render(
    <ColorSchemeProvider>
      <SocThemeProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/setup" element={<div>setup-route</div>} />
            <Route path="/:screen" element={<UserMenu onShowTour={onShowTour} />} />
          </Routes>
        </MemoryRouter>
      </SocThemeProvider>
    </ColorSchemeProvider>,
  )
}

describe('UserMenu', () => {
  beforeEach(() => {
    auth.user.username = 'dev-user'
    auth.user.full_name = 'Test User'
  })

  it('renders initials and labels from full_name', async () => {
    renderMenu()
    expect(screen.getByText('TU')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    expect(screen.getAllByText('Test User').length).toBeGreaterThan(0)
    expect(await screen.findByText('About · 9.9.9')).toBeInTheDocument()
    const feedback = screen.getByRole('menuitem', { name: 'Share feedback' })
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
    fireEvent.click(screen.getByRole('menuitem', { name: 'Console tour' }))
    expect(onShowTour).toHaveBeenCalledOnce()
    expect(screen.queryByRole('menu', { name: 'Account' })).not.toBeInTheDocument()
  })

  it('opens setup from the account menu', () => {
    renderMenu()
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Setup' }))
    expect(screen.getByText('setup-route')).toBeInTheDocument()
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
