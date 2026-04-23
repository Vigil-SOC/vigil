import { lazy, Suspense } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { Box, CircularProgress } from '@mui/material'
import { AuthProvider } from './contexts/AuthContext'
import ProtectedRoute from './components/auth/ProtectedRoute'
import MainLayout from './components/layout/MainLayout'

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

const PageFallback = () => (
  <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'center', flex: 1, minHeight: 200 }}>
    <CircularProgress size={24} />
  </Box>
)

function App() {
  return (
    <AuthProvider>
      <Box sx={{ display: 'flex', height: '100vh' }}>
        <Suspense fallback={<PageFallback />}>
        <Routes>
          {/* Public routes */}
          <Route path="/login" element={<Login />} />
          
          {/* Protected routes */}
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <MainLayout />
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

