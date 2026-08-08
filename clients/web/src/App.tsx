import { lazy, Suspense } from 'react'
import { Routes, Route, Navigate, Outlet } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import ProtectedRoute from './components/auth/ProtectedRoute'
import SetupGate from './components/setup/SetupGate'
// Eager (never suspends) so it can serve as the redesign's own Suspense
// fallback while the lazy redesign chunk loads.
import RedesignLoader from './redesign/shell/Loader'

// Lazy-loaded so a refresh on any route only pulls that screen's module graph.
const SocConsole = lazy(() => import('./redesign/SocConsole'))
const SocLogin = lazy(() => import('./redesign/screens/login/LoginScreen'))
// Standalone /setup screen (no console shell).
const SetupScreen = lazy(() => import('./redesign/screens/setup/SetupScreen'))

function App() {
  return (
    <AuthProvider>
      <div className="flex h-screen">
        <Suspense fallback={<RedesignLoader />}>
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

            {/* Back-compat — the console used to live under /redesign/*. */}
            <Route path="/redesign" element={<Navigate to="/" replace />} />
            <Route path="/redesign/*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </div>
    </AuthProvider>
  )
}

export default App
