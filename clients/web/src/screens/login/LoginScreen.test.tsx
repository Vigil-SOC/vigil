import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ColorSchemeProvider } from '../../contexts/ColorSchemeContext'
import LoginScreen from './LoginScreen'

const login = vi.fn()
const navigate = vi.fn()
const requestPasswordReset = vi.fn()
const bootstrapStatus = vi.fn(() => Promise.resolve({ data: { required: false } }))
const bootstrapCreate = vi.fn((_payload?: unknown) => Promise.resolve({ data: {} }))

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
    status: () => bootstrapStatus(),
    create: (payload: unknown) => bootstrapCreate(payload),
  },
  authApi: { requestPasswordReset: (email: string) => requestPasswordReset(email) },
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
  requestPasswordReset.mockReset()
  bootstrapStatus.mockReset()
  bootstrapStatus.mockResolvedValue({ data: { required: false } })
  bootstrapCreate.mockReset()
  bootstrapCreate.mockResolvedValue({ data: {} })
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

  it('accepts an 8-character recovery code in the MFA step', async () => {
    login.mockRejectedValueOnce(new Error('MFA_REQUIRED')).mockResolvedValueOnce(undefined)
    renderLogin()
    fireEvent.change(screen.getByLabelText('Username or email'), { target: { value: 'admin' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'admin123' } })
    fireEvent.click(screen.getByRole('button', { name: /^sign in$/i }))
    const code = await screen.findByLabelText('Authentication code')
    fireEvent.change(code, { target: { value: '3cfb-0b62' } })
    expect(code).toHaveValue('3cfb0b62')
    fireEvent.click(screen.getByRole('button', { name: /verify/i }))
    await waitFor(() => expect(login).toHaveBeenLastCalledWith('admin', 'admin123', '3cfb0b62'))
  })

  it('sends the setup code and shows the server refusal when it is wrong', async () => {
    bootstrapStatus.mockResolvedValue({ data: { required: true } })
    bootstrapCreate.mockRejectedValueOnce({
      response: { status: 403, data: { detail: 'Setup code is missing or incorrect.' } },
    })
    renderLogin()
    expect(await screen.findByRole('heading', { name: 'Create your account' })).toBeInTheDocument()
    expect(screen.getByText(/docker compose logs/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Setup code'), { target: { value: 'wrong-code' } })
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'admin' } })
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'a@b.co' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'harbour-lantern' } })
    fireEvent.click(screen.getByRole('button', { name: /create account/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Setup code is missing or incorrect.')
    expect(bootstrapCreate).toHaveBeenCalledWith({
      username: 'admin',
      email: 'a@b.co',
      password: 'harbour-lantern',
      setup_code: 'wrong-code',
    })
    expect(login).not.toHaveBeenCalled()
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

  describe('forgot password', () => {
    const GENERIC = 'If that email matches an active account, a reset link has been sent.'

    it('requests a reset and shows only the server message', async () => {
      requestPasswordReset.mockResolvedValueOnce({ data: { message: GENERIC } })
      renderLogin()
      fireEvent.click(screen.getByRole('button', { name: 'Forgot password?' }))
      expect(screen.getByRole('heading', { name: 'Reset your password' })).toBeInTheDocument()
      fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'a@b.co' } })
      fireEvent.click(screen.getByRole('button', { name: /send reset link/i }))
      expect(await screen.findByRole('alert')).toHaveTextContent(GENERIC)
      expect(requestPasswordReset).toHaveBeenCalledWith('a@b.co')
      expect(screen.queryByLabelText('Email')).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: /back to sign in/i }))
      expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    })

    it('explains a rate-limited request', async () => {
      // slowapi's 429 body carries `error`, not `detail`
      requestPasswordReset.mockRejectedValueOnce({
        response: { status: 429, data: { error: 'Rate limit exceeded: 3 per 1 hour' } },
      })
      renderLogin()
      fireEvent.click(screen.getByRole('button', { name: 'Forgot password?' }))
      fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'a@b.co' } })
      fireEvent.click(screen.getByRole('button', { name: /send reset link/i }))
      expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts')
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
