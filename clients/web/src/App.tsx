import { lazy, Suspense } from 'react'
import { Routes, Route, Navigate, Outlet } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import ProtectedRoute from './routing/ProtectedRoute'
import SetupGate from './routing/SetupGate'
// Eager (never suspends) so it can serve as the Suspense fallback while the
// lazy console/login/setup chunks load.
import Loader from './routing/Loader'

// Lazy-loaded so a refresh on any route only pulls that screen's module graph.
const SocConsole = lazy(() => import('./shell/SocConsole'))
const SocLogin = lazy(() => import('./screens/login/LoginScreen'))
// Standalone /setup screen (no console shell).
const SetupScreen = lazy(() => import('./screens/setup/SetupScreen'))

function App() {
  return (
    <AuthProvider>
      <div className="flex h-screen">
        <Suspense fallback={<Loader />}>
          <Routes>
            {/* Public — the login screen is the single sign-in surface. */}
            <Route path="/login" element={<SocLogin />} />

            {/* OUTSIDE SetupGate so it stays reachable while unconfigured (no redirect loop). */}
            <Route
              path="/setup"
              element={<ProtectedRoute><SetupScreen /></ProtectedRoute>}
            />

            {/* Primary app — the SOC console, gated behind auth + first-run setup.
                Each screen owns a URL (/<screen>); cases deep-link to a specific
                case via the ?case=<caseId> query param. */}
            <Route
              element={
                <ProtectedRoute>
                  <SetupGate>
                    <Outlet />
                  </SetupGate>
                </ProtectedRoute>
              }
            >
              <Route index element={<Navigate to="/dashboard" replace />} />
              <Route path=":screen" element={<SocConsole />} />
              {/* deeper junk paths (/a/b/…) fall through to the in-shell 404 */}
              <Route path="*" element={<SocConsole />} />
            </Route>
          </Routes>
        </Suspense>
      </div>
    </AuthProvider>
  )
}

export default App
