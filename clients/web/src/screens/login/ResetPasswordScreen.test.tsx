import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ColorSchemeProvider } from '../../contexts/ColorSchemeContext'
import ResetPasswordScreen from './ResetPasswordScreen'

const navigate = vi.fn()
const confirm = vi.fn()
const status = vi.fn()

vi.mock('../../services/api', () => ({
  configApi: {
    getTheme: () => Promise.resolve({ data: { theme: 'dark' } }),
    setTheme: () => Promise.resolve({ data: {} }),
  },
  bootstrapApi: { status: () => status() },
  passwordResetApi: { confirm: (p: unknown) => confirm(p) },
}))

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigate }
})

function renderAt(url: string) {
  return render(
    <ColorSchemeProvider>
      <MemoryRouter initialEntries={[url]}>
        <ResetPasswordScreen />
      </MemoryRouter>
    </ColorSchemeProvider>,
  )
}

function submit(password: string) {
  fireEvent.change(screen.getByLabelText('New password'), { target: { value: password } })
  fireEvent.click(screen.getByRole('button', { name: /reset password/i }))
}

beforeEach(() => {
  navigate.mockReset()
  confirm.mockReset()
  status.mockReset().mockResolvedValue({ data: { required: false } })
})

describe('ResetPasswordScreen', () => {
  it('confirms the token and routes to sign-in with a notice', async () => {
    confirm.mockResolvedValueOnce({ data: {} })
    renderAt('/reset-password?token=tok123')
    submit('correct horse battery staple')
    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({
        token: 'tok123',
        new_password: 'correct horse battery staple',
      }),
    )
    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith(
        '/login',
        expect.objectContaining({ state: { notice: expect.stringMatching(/reset/i) } }),
      ),
    )
  })

  it('shows its own state when the token is missing', () => {
    renderAt('/reset-password')
    expect(screen.getByRole('heading', { name: 'Reset link incomplete' })).toBeInTheDocument()
    expect(screen.queryByLabelText('New password')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /back to sign in/i })).toBeInTheDocument()
  })

  it('surfaces the server detail on failure', async () => {
    confirm.mockRejectedValueOnce({
      response: { status: 400, data: { detail: 'Invalid or expired reset token' } },
    })
    renderAt('/reset-password?token=bad')
    submit('whatever-long-password')
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid or expired reset token')
    expect(navigate).not.toHaveBeenCalled()
  })
})
