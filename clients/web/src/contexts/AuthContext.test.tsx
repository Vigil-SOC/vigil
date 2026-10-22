import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { ColorSchemeProvider } from './ColorSchemeContext'
import { AuthProvider, useAuth } from './AuthContext'
import ProtectedRoute from '../routing/ProtectedRoute'
import { saveHistory } from '../shell/chatCache'

const get = vi.fn()
const post = vi.fn()

vi.mock('../services/api', () => ({
  default: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) },
  configApi: {
    getTheme: () => Promise.resolve({ data: { theme: 'dark' } }),
    setTheme: () => Promise.resolve({ data: {} }),
  },
}))

function Where() {
  return <div data-testid="path">{useLocation().pathname}</div>
}

function renderAt(path: string) {
  return render(
    <ColorSchemeProvider>
      <AuthProvider>
        <MemoryRouter initialEntries={[path]}>
          <Where />
          <Routes>
            <Route path="/login" element={<div>login screen</div>} />
            <Route
              path="/cases"
              element={
                <ProtectedRoute>
                  <div>cases page</div>
                </ProtectedRoute>
              }
            />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </ColorSchemeProvider>,
  )
}

function SessionButtons() {
  const { login, logout } = useAuth()
  return (
    <>
      <button onClick={() => login('bob', 'pw')}>do login</button>
      <button onClick={() => logout()}>do logout</button>
    </>
  )
}

beforeEach(() => {
  localStorage.clear()
  get.mockReset()
  post.mockReset()
})

describe('AuthProvider /auth/me handling', () => {
  // 403 is the failed /auth/refresh retry before the csrf cookie exists
  it.each([401, 403])('redirects to /login on a %i', async (status) => {
    get.mockRejectedValueOnce({ response: { status } })
    renderAt('/cases')
    expect(await screen.findByText('login screen')).toBeInTheDocument()
  })

  it.each([
    ['a network error', new Error('Network Error')],
    ['a 5xx', { response: { status: 500 } }],
  ])('shows the unreachable state on %s without leaving the page', async (_name, err) => {
    get.mockRejectedValueOnce(err)
    renderAt('/cases')
    expect(await screen.findByText("Can't reach Vigil")).toBeInTheDocument()
    expect(screen.getByTestId('path')).toHaveTextContent('/cases')
    expect(screen.queryByText('login screen')).not.toBeInTheDocument()
  })

  it('keeps the chat cache when the session cannot be checked', async () => {
    saveHistory('user-1', [{ id: 'c1', title: 'Mine', ts: 1, messages: [{ role: 'user', text: 'secret' }] }])
    get.mockRejectedValueOnce(new Error('Network Error'))
    renderAt('/cases')
    expect(await screen.findByText("Can't reach Vigil")).toBeInTheDocument()
    expect(localStorage.getItem('soc.chat.history.user-1')).not.toBeNull()
  })

  it('clears every user\'s chat cache when the session is refused', async () => {
    saveHistory('user-1', [{ id: 'c1', title: 'Mine', ts: 1, messages: [{ role: 'user', text: 'secret' }] }])
    localStorage.setItem('soc.chat.history', '[{"id":"legacy"}]')
    localStorage.setItem('soc.chat.threadHeight', '400')
    get.mockRejectedValueOnce({ response: { status: 401 } })
    renderAt('/cases')
    expect(await screen.findByText('login screen')).toBeInTheDocument()
    expect(localStorage.getItem('soc.chat.history.user-1')).toBeNull()
    expect(localStorage.getItem('soc.chat.history')).toBeNull()
    expect(localStorage.getItem('soc.chat.threadHeight')).toBe('400')
  })

  it('keeps the signed-in user\'s cache and drops everyone else\'s', async () => {
    saveHistory('user-1', [{ id: 'c1', title: 'Mine', ts: 1, messages: [{ role: 'user', text: 'secret' }] }])
    saveHistory('user-2', [{ id: 'c2', title: 'Other', ts: 1, messages: [{ role: 'user', text: 'other' }] }])
    localStorage.setItem('soc.chat.history', '[{"id":"legacy"}]')
    get.mockResolvedValueOnce({ data: { user_id: 'user-1', username: 'alice' } })
    renderAt('/cases')
    expect(await screen.findByText('cases page')).toBeInTheDocument()
    expect(localStorage.getItem('soc.chat.history.user-1')).not.toBeNull()
    expect(localStorage.getItem('soc.chat.history.user-2')).toBeNull()
    expect(localStorage.getItem('soc.chat.history')).toBeNull()
  })

  it('drops the previous user on sign-in and clears the cache on sign-out', async () => {
    get.mockRejectedValueOnce({ response: { status: 401 } })
    post.mockImplementation((url: string) => {
      if (url === '/auth/login') {
        return Promise.resolve({ data: { user: { user_id: 'user-2', username: 'bob' } } })
      }
      return Promise.resolve({ data: {} })
    })
    render(
      <ColorSchemeProvider>
        <AuthProvider>
          <SessionButtons />
        </AuthProvider>
      </ColorSchemeProvider>,
    )
    await waitFor(() => expect(get).toHaveBeenCalled())
    await act(async () => {
      await Promise.resolve()
    })
    saveHistory('user-1', [{ id: 'c1', title: 'Mine', ts: 1, messages: [{ role: 'user', text: 'secret' }] }])
    saveHistory('user-2', [{ id: 'c2', title: 'Bob', ts: 1, messages: [{ role: 'user', text: 'bob' }] }])
    fireEvent.click(screen.getByRole('button', { name: 'do login' }))
    await waitFor(() => expect(localStorage.getItem('soc.chat.history.user-1')).toBeNull())
    expect(localStorage.getItem('soc.chat.history.user-2')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'do logout' }))
    await waitFor(() => expect(localStorage.getItem('soc.chat.history.user-2')).toBeNull())
  })

  it('Retry re-runs /auth/me and lands on the requested page', async () => {
    get.mockRejectedValueOnce(new Error('Network Error'))
    get.mockResolvedValueOnce({ data: { user_id: 'u1', username: 'admin' } })
    renderAt('/cases')
    fireEvent.click(await screen.findByRole('button', { name: /retry/i }))
    expect(await screen.findByText('cases page')).toBeInTheDocument()
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
  })
})
