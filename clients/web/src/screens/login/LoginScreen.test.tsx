import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ColorSchemeProvider } from '../../contexts/ColorSchemeContext'
import LoginScreen from './LoginScreen'

const login = vi.fn()
const navigate = vi.fn()

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ login }),
}))

// stubbed so the hydrate effect resolves deterministically in jsdom
vi.mock('../../services/api', () => ({
  configApi: {
    getTheme: () => Promise.resolve({ data: { theme: 'dark' } }),
    setTheme: () => Promise.resolve({ data: {} }),
  },
  // unmocked, this throws inside the mount effect and fails every test here
  bootstrapApi: {
    status: () => Promise.resolve({ data: { required: false } }),
    create: () => Promise.resolve({ data: {} }),
  },
}))

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigate }
})

function renderLogin() {
  return render(
    <ColorSchemeProvider>
      <MemoryRouter initialEntries={['/login']}>
        <LoginScreen />
      </MemoryRouter>
    </ColorSchemeProvider>,
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
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/'))
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

  describe('sign-in errors', () => {
    async function submit() {
      renderLogin()
      fireEvent.change(screen.getByLabelText('Username or email'), { target: { value: 'admin' } })
      fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw' } })
      fireEvent.click(screen.getByRole('button', { name: /^sign in$/i }))
      return screen.findByRole('alert')
    }

    it('blames the backend, not the credentials, on a network error', async () => {
      login.mockRejectedValueOnce(new Error('Network Error'))
      expect(await submit()).toHaveTextContent("Can't reach the Vigil API. Is the backend running?")
    })

    it('treats a 5xx without a detail as unreachable', async () => {
      login.mockRejectedValueOnce({ response: { status: 500, data: '' } })
      expect(await submit()).toHaveTextContent("Can't reach the Vigil API")
    })

    it('shows the server detail on a 5xx that has one', async () => {
      login.mockRejectedValueOnce({ response: { status: 500, data: { detail: 'DB locked' } } })
      expect(await submit()).toHaveTextContent('DB locked')
    })

    it('keeps the credentials message on a 401', async () => {
      login.mockRejectedValueOnce({ response: { status: 401, data: {} } })
      expect(await submit()).toHaveTextContent('Sign in failed. Check your credentials.')
    })
  })

  it('toggles between light and dark mode', async () => {
    const { container } = renderLogin()
    const root = container.querySelector('.auth-root') as HTMLElement
    await waitFor(() => expect(root.getAttribute('data-theme')).toBe('dark'))
    expect(root).toHaveClass('vg-dark')
    fireEvent.click(screen.getByRole('button', { name: /switch to light mode/i }))
    await waitFor(() => expect(root.getAttribute('data-theme')).toBe('light'))
    expect(root).toHaveClass('vg-light')
  })
})
