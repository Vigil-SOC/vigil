import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import TwoStepDialog from './TwoStepDialog'

const mfa = vi.hoisted(() => ({
  setup: vi.fn(),
  verify: vi.fn(),
  regenerateRecoveryCodes: vi.fn(),
  disable: vi.fn(),
}))
vi.mock('../services/api', () => ({ mfaApi: mfa }))
vi.mock('qrcode', () => ({ default: { toDataURL: vi.fn().mockResolvedValue('data:image/png;base64,QR') } }))

const auth = vi.hoisted(() => ({
  user: { mfa_enabled: false },
  refreshUser: vi.fn(),
}))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => auth }))

const CODES = ['AAAA1111', 'BBBB2222']
const apiError = (status: number, detail: string) => ({ response: { status, data: { detail } } })

function renderDialog() {
  return render(<TwoStepDialog open onClose={vi.fn()} />)
}

describe('TwoStepDialog', () => {
  beforeEach(() => {
    auth.user.mfa_enabled = false
    auth.refreshUser.mockReset().mockResolvedValue(undefined)
    Object.values(mfa).forEach((fn) => fn.mockReset())
  })

  it('off: enrols through setup, QR + secret, verify, then shows recovery codes once', async () => {
    mfa.setup.mockResolvedValue({ data: { secret: 'JBSWY3DPEHPK3PXP', qr_uri: 'otpauth://totp/x' } })
    mfa.verify.mockResolvedValue({ data: { recovery_codes: CODES } })
    renderDialog()

    fireEvent.click(screen.getByRole('button', { name: 'Set up' }))
    expect(await screen.findByLabelText('Setup key')).toHaveTextContent('JBSWY3DPEHPK3PXP')
    expect(screen.getByAltText(/QR code/)).toHaveAttribute('src', 'data:image/png;base64,QR')

    fireEvent.change(screen.getByRole('textbox'), { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))

    expect(await screen.findByText(/cannot be shown again/)).toBeInTheDocument()
    expect(mfa.verify).toHaveBeenCalledWith('123456')
    expect(screen.getByLabelText('Your recovery codes')).toHaveTextContent('AAAA1111')
    expect(screen.getByRole('button', { name: /Copy/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Download/ })).toBeInTheDocument()
    expect(auth.refreshUser).toHaveBeenCalled()
  })

  it('shows a setup failure inline instead of crashing', async () => {
    mfa.setup.mockRejectedValue(apiError(500, 'Failed to setup MFA'))
    renderDialog()
    fireEvent.click(screen.getByRole('button', { name: 'Set up' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to setup MFA')
    expect(screen.getByRole('button', { name: 'Set up' })).toBeInTheDocument()
  })

  it('shows a wrong code and the rate limit inline, staying on the code step', async () => {
    mfa.setup.mockResolvedValue({ data: { secret: 'S', qr_uri: 'otpauth://totp/x' } })
    mfa.verify.mockRejectedValueOnce(apiError(400, 'Invalid MFA code'))
    mfa.verify.mockRejectedValueOnce(apiError(429, '5 per 1 minute'))
    renderDialog()
    fireEvent.click(screen.getByRole('button', { name: 'Set up' }))
    await screen.findByLabelText('Setup key')
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '000000' } })

    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid MFA code')

    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Too many attempts'))
    expect(auth.refreshUser).not.toHaveBeenCalled()
  })

  it('on: regenerates recovery codes after confirming', async () => {
    auth.user.mfa_enabled = true
    mfa.regenerateRecoveryCodes.mockResolvedValue({ data: { recovery_codes: CODES } })
    renderDialog()

    expect(screen.getByText('Two-step sign-in is on.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate recovery codes' }))
    expect(mfa.regenerateRecoveryCodes).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }))

    expect(await screen.findByLabelText('Your recovery codes')).toHaveTextContent('BBBB2222')
  })

  it('on: turning off sends the code and refreshes the user; a bad code is refused inline', async () => {
    auth.user.mfa_enabled = true
    mfa.disable.mockRejectedValueOnce(apiError(400, 'Invalid MFA code or MFA is not enabled'))
    mfa.disable.mockResolvedValueOnce({ data: { message: 'ok' } })
    renderDialog()

    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '000000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid MFA code')
    expect(auth.refreshUser).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: 'AAAA1111' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    await waitFor(() => expect(auth.refreshUser).toHaveBeenCalled())
    expect(mfa.disable).toHaveBeenLastCalledWith('AAAA1111')
  })
})
