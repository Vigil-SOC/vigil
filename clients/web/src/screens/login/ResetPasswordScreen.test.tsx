import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ColorSchemeProvider } from '../../contexts/ColorSchemeContext'
import ResetPasswordScreen from './ResetPasswordScreen'

const navigate = vi.fn()
const confirmPasswordReset = vi.fn()

vi.mock('../../services/api', () => ({
  configApi: {
    getTheme: () => Promise.resolve({ data: { theme: 'dark' } }),
    setTheme: () => Promise.resolve({ data: {} }),
  },
  authApi: { confirmPasswordReset: (p: unknown) => confirmPasswordReset(p) },
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

function submit(pw: string) {
  fireEvent.change(screen.getByLabelText('New password'), { target: { value: pw } })
  fireEvent.click(screen.getByRole('button', { name: /set new password/i }))
}

beforeEach(() => {
  navigate.mockReset()
  confirmPasswordReset.mockReset()
})

describe('ResetPasswordScreen', () => {
  it('confirms with the URL token and hands off to sign-in', async () => {
    confirmPasswordReset.mockResolvedValueOnce({ data: { message: 'ok' } })
    renderAt('/reset-password?token=tok123')
    submit('a-long-new-passphrase')
    await waitFor(() =>
      expect(confirmPasswordReset).toHaveBeenCalledWith({
        token: 'tok123',
        new_password: 'a-long-new-passphrase',
      }),
    )
    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith('/login', {
        replace: true,
        state: { notice: 'Password updated. Sign in with your new password.' },
      }),
    )
  })

  it('shows the server detail and retries with the same token after a rejected password', async () => {
    confirmPasswordReset
      .mockRejectedValueOnce({
        response: { status: 400, data: { detail: 'Password is too short (Add 4 more characters.)' } },
      })
      .mockResolvedValueOnce({ data: { message: 'ok' } })
    renderAt('/reset-password?token=tok123')
    submit('short')
    expect(await screen.findByRole('alert')).toHaveTextContent('Password is too short')
    expect(navigate).not.toHaveBeenCalled()
    submit('a-long-new-passphrase')
    await waitFor(() => expect(navigate).toHaveBeenCalled())
    expect(confirmPasswordReset).toHaveBeenLastCalledWith({
      token: 'tok123',
      new_password: 'a-long-new-passphrase',
    })
  })

  it('shows the server detail for a rejected token and offers a new link', async () => {
    confirmPasswordReset.mockRejectedValueOnce({
      response: { status: 400, data: { detail: 'Invalid or expired reset token' } },
    })
    renderAt('/reset-password?token=tok123')
    submit('a-long-new-passphrase')
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid or expired reset token')
    fireEvent.click(screen.getByRole('button', { name: /request a new link/i }))
    expect(navigate).toHaveBeenCalledWith('/login', { state: { forgot: true } })
  })

  it('keeps the form for a retry when the API is unreachable', async () => {
    confirmPasswordReset.mockRejectedValueOnce(new Error('Network Error'))
    renderAt('/reset-password?token=tok123')
    submit('a-long-new-passphrase')
    expect(await screen.findByRole('alert')).toHaveTextContent("Can't reach the Vigil API")
    expect(screen.getByLabelText('New password')).toBeInTheDocument()
  })

  it('has its own state when the token is missing', () => {
    renderAt('/reset-password')
    expect(screen.getByRole('heading', { name: 'Reset link incomplete' })).toBeInTheDocument()
    expect(screen.queryByLabelText('New password')).not.toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: /request a new link/i })[0])
    expect(navigate).toHaveBeenCalledWith('/login', { state: { forgot: true } })
  })
})
