import { lazy, Suspense } from 'react'
import { Routes, Route, Navigate, Outlet } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import ProtectedRoute from './routing/ProtectedRoute'
import SetupGate from './routing/SetupGate'
// eager, so it can be the Suspense fallback for the lazy chunks below
import Loader from './routing/Loader'

// lazy, so a refresh on any route only pulls that screen's module graph
const SocConsole = lazy(() => import('./shell/SocConsole'))
const SocLogin = lazy(() => import('./screens/login/LoginScreen'))
const SetupScreen = lazy(() => import('./screens/setup/SetupScreen'))

function App() {
  return (
    <AuthProvider>
      {/* No layout wrapper: every routed surface — console, login, setup and
          the Loader — mounts its own `.soc-console` root, which is
          `position: fixed; inset: 0`. A sized wrapper here holds nothing but
          out-of-flow children, so its height is dead space the document has
          to scroll past. */}
      <Suspense fallback={<Loader />}>
        <Routes>
          {/* Public — the login screen is the single sign-in surface. */}
          <Route path="/login" element={<SocLogin />} />

          {/* OUTSIDE SetupGate so it stays reachable while unconfigured (no redirect loop). */}
          <Route
            path="/setup"
            element={<ProtectedRoute><SetupScreen /></ProtectedRoute>}
          />

          {/* Primary app — the SOC console, behind auth. First visit (no
              vigil.setupDismissed) is sent to /setup once. Each screen owns a
              URL (/<screen>); cases deep-link via ?case=<caseId>. */}
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
    </AuthProvider>
  )
}

export default App
