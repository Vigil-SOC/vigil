import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ColorSchemeProvider } from '../contexts/ColorSchemeContext'
import UserMenu from './UserMenu'

const api = vi.hoisted(() => ({
  changePassword: vi.fn(),
}))

vi.mock('../services/api', () => ({
  configApi: {
    getTheme: () => Promise.resolve({ data: { theme: 'dark' } }),
    setTheme: () => Promise.resolve({ data: {} }),
  },
  authApi: { changePassword: api.changePassword },
}))

const auth = vi.hoisted(() => ({
  user: {
    username: 'dev-user',
    full_name: 'Test User',
    email: 'dev@localhost',
    role_id: 'role-admin',
    mfa_enabled: false,
  },
  logout: vi.fn(),
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => auth,
}))

const CURRENT = 'old-password-1'
const NEXT = 'correct horse battery staple'

function renderMenu(authBypassed = false) {
  return render(
    <ColorSchemeProvider>
      <MemoryRouter initialEntries={['/cases']}>
        <Routes>
          <Route path="/login" element={<div>login-route</div>} />
          <Route path="/:screen" element={<UserMenu onShowTour={vi.fn()} setupLeft={null} authBypassed={authBypassed} version="9.9.9" />} />
        </Routes>
      </MemoryRouter>
    </ColorSchemeProvider>,
  )
}

async function openDialog() {
  renderMenu()
  fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Change password' }))
  return screen.getByRole('dialog', { name: 'Change password' })
}

const field = (name: RegExp) => screen.getByLabelText(name, { selector: 'input' }) as HTMLInputElement
const type = (name: RegExp, value: string) => fireEvent.change(field(name), { target: { value } })
const submit = () => screen.getByRole('button', { name: 'Change password' })

function fillValid() {
  type(/^Current password/, CURRENT)
  type(/^New password/, NEXT)
  type(/^Confirm new password/, NEXT)
}

describe('ChangePasswordDialog', () => {
  beforeEach(() => {
    api.changePassword.mockReset()
    auth.logout.mockReset().mockResolvedValue(undefined)
  })

  it('opens from the account menu, closes the menu and focuses the first field', async () => {
    const dialog = await openDialog()
    expect(screen.queryByRole('menu', { name: 'Account' })).not.toBeInTheDocument()
    expect(dialog).toHaveTextContent('signed out on every device')
    await waitFor(() => expect(field(/^Current password/)).toHaveFocus())
    expect(field(/^Current password/)).toHaveAttribute('autocomplete', 'current-password')
    expect(field(/^New password/)).toHaveAttribute('autocomplete', 'new-password')
  })

  it('closes on Cancel and on Escape, returning focus to the avatar', async () => {
    await openDialog()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Account menu' })).toHaveFocus()

    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Change password' }))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(api.changePassword).not.toHaveBeenCalled()
  })

  it('keeps submit disabled and says why until the form is valid', async () => {
    await openDialog()
    expect(submit()).toBeDisabled()

    type(/^Current password/, CURRENT)
    type(/^New password/, CURRENT)
    fireEvent.blur(field(/^New password/))
    expect(screen.getByText('Choose a password different from your current one.')).toBeInTheDocument()
    expect(field(/^New password/)).toHaveAttribute('aria-invalid', 'true')
    expect(submit()).toBeDisabled()

    type(/^New password/, NEXT)
    expect(screen.queryByText(/different from your current one/)).not.toBeInTheDocument()
    type(/^Confirm new password/, NEXT.replace('staple', 'stapler'))
    expect(screen.getByText('This does not match the new password.')).toBeInTheDocument()
    expect(submit()).toBeDisabled()

    type(/^Confirm new password/, NEXT)
    expect(screen.queryByText('This does not match the new password.')).not.toBeInTheDocument()
    expect(submit()).toBeEnabled()
  })

  it('changes the password, says the user is signed out, then signs out to the login page', async () => {
    api.changePassword.mockResolvedValue({ data: { message: 'Password changed successfully. Please log in again.' } })
    await openDialog()
    fillValid()
    fireEvent.click(submit())

    const done = await screen.findByRole('dialog', { name: 'Password changed' })
    expect(api.changePassword).toHaveBeenCalledWith({ current_password: CURRENT, new_password: NEXT })
    expect(done).toHaveTextContent('signed out on every device')
    expect(done).toHaveTextContent('Sign in again with your new password')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in again' })).toHaveFocus())
    expect(auth.logout).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Sign in again' }))
    await waitFor(() => expect(auth.logout).toHaveBeenCalledOnce())
    expect(await screen.findByText('login-route')).toBeInTheDocument()
  })

  it('stays open while the change is in flight', async () => {
    let finish: (value: unknown) => void = () => {}
    api.changePassword.mockReturnValue(new Promise((resolve) => { finish = resolve }))
    await openDialog()
    fillValid()
    fireEvent.click(submit())
    expect(await screen.findByRole('button', { name: 'Changing…' })).toBeDisabled()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByRole('dialog', { name: 'Change password' })).toBeInTheDocument()
    finish({ data: { message: 'ok' } })
    expect(await screen.findByRole('dialog', { name: 'Password changed' })).toBeInTheDocument()
  })

  it('signs out when the success notice is dismissed with Escape', async () => {
    api.changePassword.mockResolvedValue({ data: { message: 'ok' } })
    await openDialog()
    fillValid()
    fireEvent.click(submit())
    await screen.findByRole('dialog', { name: 'Password changed' })
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(auth.logout).toHaveBeenCalledOnce())
  })

  it("shows the server's message and keeps the user signed in when the change is refused", async () => {
    // 400, not 401: the session is valid, only a body field is wrong (#2224)
    api.changePassword.mockRejectedValue({
      response: { status: 400, data: { detail: 'Current password is incorrect' } },
    })
    await openDialog()
    fillValid()
    fireEvent.click(submit())

    expect(await screen.findByRole('alert')).toHaveTextContent('Current password is incorrect')
    expect(screen.getByRole('dialog', { name: 'Change password' })).toBeInTheDocument()
    expect(field(/^New password/).value).toBe(NEXT)
    expect(auth.logout).not.toHaveBeenCalled()

    // editing the form clears the stale message
    type(/^Current password/, 'another-try-1')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it("leaves length to the server and shows its message, so a deployment's own floor applies", async () => {
    api.changePassword.mockRejectedValue({
      response: { status: 400, data: { detail: 'Password must be at least 12 characters long (Add 3 more characters.)' } },
    })
    await openDialog()
    type(/^Current password/, CURRENT)
    type(/^New password/, 'too-short')
    type(/^Confirm new password/, 'too-short')
    expect(submit()).toBeEnabled()
    fireEvent.click(submit())
    expect(await screen.findByRole('alert')).toHaveTextContent('Password must be at least 12 characters long')
    expect(api.changePassword).toHaveBeenCalledWith({ current_password: CURRENT, new_password: 'too-short' })
  })

  it('shows the strength policy message the server sends', async () => {
    api.changePassword.mockRejectedValue({
      response: { status: 400, data: { detail: 'Password too weak (strength 1/4, need 3/4) (Add another word or two.)' } },
    })
    await openDialog()
    fillValid()
    fireEvent.click(submit())
    expect(await screen.findByRole('alert')).toHaveTextContent('Password too weak (strength 1/4, need 3/4)')
  })

  it('explains a rate limit, which comes back without a detail', async () => {
    api.changePassword.mockRejectedValue({
      response: { status: 429, data: { error: 'Rate limit exceeded: 5 per 1 minute' } },
    })
    await openDialog()
    fillValid()
    fireEvent.click(submit())
    expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts. Wait a while and try again.')
  })

  it('leaves the item out when auth is bypassed', async () => {
    renderMenu(true)
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }))
    expect(await screen.findByText('About Vigil · 9.9.9')).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Change password' })).not.toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Sign out' })).toBeInTheDocument()
  })
})
