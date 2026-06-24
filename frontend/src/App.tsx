import { lazy, Suspense } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { Box, CircularProgress } from '@mui/material'
import { AuthProvider } from './contexts/AuthContext'
import ProtectedRoute from './components/auth/ProtectedRoute'
import MainLayout from './components/layout/MainLayout'
import SetupGate from './components/setup/SetupGate'
// Eager (never suspends) so it can serve as the redesign's own Suspense
// fallback while the lazy /redesign chunk loads.
import RedesignLoader from './redesign/shell/Loader'

// Lazy-load every page so a refresh on any route only pulls that page's
// module graph (plus shared deps). Previously every page was eagerly
// imported, forcing ~1 MB of JS + all its MUI/recharts/x-data-grid deps
// on every cold load.
const Login = lazy(() => import('./pages/Login'))
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Cases = lazy(() => import('./pages/Cases'))
const CaseMetrics = lazy(() => import('./pages/CaseMetrics'))
const Timesketch = lazy(() => import('./pages/Timesketch'))
const Settings = lazy(() => import('./pages/Settings'))
const AIDecisions = lazy(() => import('./pages/AIDecisions'))
const Investigation = lazy(() => import('./pages/Investigation'))
const Analytics = lazy(() => import('./pages/Analytics'))
const Skills = lazy(() => import('./pages/Skills'))
const Orchestrator = lazy(() => import('./pages/Orchestrator'))
const BuilderTool = lazy(() => import('./pages/BuilderTool'))
// UI redesign preview (Claude Design handoff) — full-screen, mock data.
const SocConsole = lazy(() => import('./redesign/SocConsole'))
const SocLogin = lazy(() => import('./redesign/screens/login/LoginScreen'))
// Standalone /setup screen (no console shell).
const SetupScreen = lazy(() => import('./redesign/screens/setup/SetupScreen'))

const PageFallback = () => (
  <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'center', flex: 1, minHeight: 200 }}>
    <CircularProgress size={24} />
  </Box>
)

// Wrap the lazy /redesign elements in their own Suspense so they show the
// redesign-styled loader (not the legacy MUI spinner) while loading.
const redesign = (el: JSX.Element) => <Suspense fallback={<RedesignLoader />}>{el}</Suspense>

function App() {
  return (
    <AuthProvider>
      <Box sx={{ display: 'flex', height: '100vh' }}>
        <Suspense fallback={<PageFallback />}>
        <Routes>
          {/* Public routes */}
          <Route path="/login" element={<Login />} />

          {/* OUTSIDE SetupGate so it stays reachable while unconfigured (no redirect loop). */}
          <Route
            path="/setup"
            element={<ProtectedRoute>{redesign(<SetupScreen />)}</ProtectedRoute>}
          />

          {/* UI redesign preview — standalone, full-screen, illustrative mock data.
              Each screen owns a URL (/redesign/<screen>); cases deep-link to a
              specific case via the ?case=<caseId> query param. */}
          <Route path="/redesign" element={<Navigate to="/redesign/dashboard" replace />} />
          <Route path="/redesign/login" element={redesign(<SocLogin />)} />
          <Route path="/redesign/:screen" element={redesign(<SocConsole />)} />
          {/* deeper junk paths (/redesign/a/b/…) fall through to the in-shell 404 */}
          <Route path="/redesign/*" element={redesign(<SocConsole />)} />


          {/* Protected routes */}
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <SetupGate>
                  <MainLayout />
                </SetupGate>
              </ProtectedRoute>
            }
          >
            <Route index element={<Dashboard />} />
            <Route
              path="cases"
              element={
                <ProtectedRoute requiredPermission="cases.read">
                  <Cases />
                </ProtectedRoute>
              }
            />
            <Route path="case-metrics" element={<CaseMetrics />} />
            <Route path="investigation" element={<Investigation />} />
            <Route path="timesketch" element={<Timesketch />} />
            <Route path="analytics" element={<Analytics />} />
            <Route path="analytics/cost" element={<Navigate to="/settings?tab=general" replace />} />
            <Route path="skills" element={<Skills />} />
            <Route path="builder" element={<BuilderTool />} />
            <Route path="workflow-builder" element={<Navigate to="/builder" replace />} />
            <Route path="orchestrator" element={<Orchestrator />} />
            <Route
              path="ai-decisions"
              element={
                <ProtectedRoute requiredPermission="ai_decisions.approve">
                  <AIDecisions />
                </ProtectedRoute>
              }
            />
            <Route
              path="settings"
              element={
                <ProtectedRoute requiredPermission="settings.read">
                  <Settings />
                </ProtectedRoute>
              }
            />
            <Route
              path="users"
              element={
                <ProtectedRoute requiredPermission="users.read">
                  <Navigate to="/settings?tab=users" replace />
                </ProtectedRoute>
              }
            />
          </Route>
        </Routes>
        </Suspense>
      </Box>
    </AuthProvider>
  )
}

export default App

