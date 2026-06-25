/**
 * Tests for the redesign login screen: it renders the credential form, wires
 * the real auth flow (useAuth().login → navigate into the console), surfaces
 * the MFA step, and toggles light/dark mode.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ThemeProvider } from '../../../contexts/ThemeContext'
import LoginScreen from './LoginScreen'

const login = vi.fn()
const navigate = vi.fn()

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ login }),
}))

// ThemeProvider hydrates/persists the mode through configApi; stub it so the
// effect resolves deterministically in jsdom (no network).
vi.mock('../../../services/api', () => ({
  configApi: {
    getTheme: () => Promise.resolve({ data: { theme: 'dark' } }),
    setTheme: () => Promise.resolve({ data: {} }),
  },
}))

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigate }
})

function renderLogin() {
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={['/login']}>
        <LoginScreen />
      </MemoryRouter>
    </ThemeProvider>,
  )
}

beforeEach(() => {
  login.mockReset()
  navigate.mockReset()
})

describe('LoginScreen', () => {
  it('renders the credential form and brand panel', () => {
    renderLogin()
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByLabelText('Username or email')).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
  })

  it('signs in and routes into the console', async () => {
    login.mockResolvedValueOnce(undefined)
    renderLogin()
    fireEvent.change(screen.getByLabelText('Username or email'), { target: { value: 'admin' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'admin123' } })
    fireEvent.click(screen.getByRole('button', { name: /^sign in$/i }))
    await waitFor(() => expect(login).toHaveBeenCalledWith('admin', 'admin123', undefined))
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/dashboard'))
  })

  it('reveals the MFA step when the backend requires it', async () => {
    login.mockRejectedValueOnce(new Error('MFA_REQUIRED'))
    renderLogin()
    fireEvent.change(screen.getByLabelText('Username or email'), { target: { value: 'admin' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'admin123' } })
    fireEvent.click(screen.getByRole('button', { name: /^sign in$/i }))
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /two-factor/i })).toBeInTheDocument(),
    )
    expect(screen.getByLabelText('Authentication code')).toBeInTheDocument()
  })

  it('toggles between light and dark mode', async () => {
    const { container } = renderLogin()
    const root = container.querySelector('.auth-root') as HTMLElement
    await waitFor(() => expect(root.getAttribute('data-theme')).toBe('dark'))
    fireEvent.click(screen.getByRole('button', { name: /switch to light mode/i }))
    await waitFor(() => expect(root.getAttribute('data-theme')).toBe('light'))
  })
})
