import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { ColorSchemeProvider } from './ColorSchemeContext'
import { AuthProvider } from './AuthContext'
import ProtectedRoute from '../routing/ProtectedRoute'

const get = vi.fn()

vi.mock('../services/api', () => ({
  default: { get: (...args: unknown[]) => get(...args), post: vi.fn() },
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

beforeEach(() => get.mockReset())

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

  it('Retry re-runs /auth/me and lands on the requested page', async () => {
    get.mockRejectedValueOnce(new Error('Network Error'))
    get.mockResolvedValueOnce({ data: { user_id: 'u1', username: 'admin' } })
    renderAt('/cases')
    fireEvent.click(await screen.findByRole('button', { name: /retry/i }))
    expect(await screen.findByText('cases page')).toBeInTheDocument()
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
  })
})
