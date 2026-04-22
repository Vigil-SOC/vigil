import { Routes, Route, Navigate } from 'react-router-dom'
import { Box } from '@mui/material'
import { AuthProvider } from './contexts/AuthContext'
import ProtectedRoute from './components/auth/ProtectedRoute'
import MainLayout from './components/layout/MainLayout'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Cases from './pages/Cases'
import CaseMetrics from './pages/CaseMetrics'
import Timesketch from './pages/Timesketch'
import Settings from './pages/Settings'
import AIDecisions from './pages/AIDecisions'
import Investigation from './pages/Investigation'
import Analytics from './pages/Analytics'
import CostAnalytics from './pages/CostAnalytics'
import Skills from './pages/Skills'
import Orchestrator from './pages/Orchestrator'
import WorkflowBuilder from './pages/WorkflowBuilder'

function App() {
  return (
    <AuthProvider>
      <Box sx={{ display: 'flex', height: '100vh' }}>
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
            <Route path="analytics/cost" element={<CostAnalytics />} />
            <Route path="skills" element={<Skills />} />
            <Route path="workflow-builder" element={<WorkflowBuilder />} />
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
      </Box>
    </AuthProvider>
  )
}

export default App

