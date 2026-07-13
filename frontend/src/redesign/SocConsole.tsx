/* ============================================================
   SOC Console — shell: nav rail, topbar, view router, Vigil chat
   dock, floating "Ask Vigil" FAB, and the theme tweaks panel.
   Ported from the design's index HTML + main.js.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import './styles.css'
import { useAuth } from '../contexts/AuthContext'
import { configApi, orchestratorApi } from '../services/api'
import { Icon } from './shared/icons'
import { NAV, TITLES, type ScreenKey } from './data/data'
import { accentVars } from './shell/accent'
import Chat from './shell/Chat'
import UserMenu from './shell/UserMenu'
import ErrorBoundary from './shell/ErrorBoundary'
import { ToastProvider } from './shell/toast'
import { useDesktopNotifications } from './shell/useDesktopNotifications'
import { RedesignThemeProvider, useSocTheme } from './shell/theme'
import type { ScreenProps } from './shared/types'
import DashboardScreen from './screens/dashboard/DashboardScreen'
import CasesScreen from './screens/cases/CasesScreen'
import MetricsScreen from './screens/metrics/MetricsScreen'
import AnalyticsScreen from './screens/analytics/AnalyticsScreen'
import DecisionsScreen from './screens/decisions/DecisionsScreen'
import WorkflowsScreen from './screens/workflows/WorkflowsScreen'
import AutoOpsScreen from './screens/autoops/AutoOpsScreen'
import SettingsScreen from './screens/settings/SettingsScreen'
import NotFoundScreen from './screens/notfound/NotFoundScreen'
import { VigilMark, VigilLogo } from './shared/VigilLogo'

const SCREENS: Record<ScreenKey, (props: ScreenProps) => JSX.Element> = {
  dashboard: DashboardScreen,
  cases: CasesScreen,
  metrics: MetricsScreen,
  analytics: AnalyticsScreen,
  decisions: DecisionsScreen,
  workflows: WorkflowsScreen,
  autoops: AutoOpsScreen,
  settings: SettingsScreen,
}

const SCREEN_KEYS = Object.keys(SCREENS) as ScreenKey[]
const isScreenKey = (s: string | undefined): s is ScreenKey =>
  s !== undefined && (SCREEN_KEYS as string[]).includes(s)

/** Per-screen permission gate, mirroring the production ProtectedRoute routes
 *  (App.tsx). Screens absent here are ungated. In DEV_MODE the auth context
 *  grants every permission, so all items show in the preview. */
const SCREEN_PERMS: Partial<Record<ScreenKey, string>> = {
  cases: 'cases.read',
  decisions: 'ai_decisions.approve',
  settings: 'settings.read',
}

export default function SocConsole() {
  // the theme provider is the single source of truth for mode + accent, read
  // here and written from the Appearance settings page; it must wrap the inner
  // shell (which both styles .soc-console and renders the settings screen).
  return (
    <RedesignThemeProvider>
      <SocConsoleInner />
    </RedesignThemeProvider>
  )
}

function SocConsoleInner() {
  // the active screen comes from the URL (/<screen>); the cases screen
  // additionally carries its open case in a ?case=<id> query param.
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const { screen } = useParams<{ screen?: string }>()
  // an unknown segment (e.g. /foo) renders the 404 screen; `current`
  // falls back to dashboard only so the chrome has a valid key to render.
  const valid = isScreenKey(screen)
  const current: ScreenKey = valid ? screen : 'dashboard'
  // whether the user may view the current screen (DEV_MODE → always true)
  const currentPerm = valid ? SCREEN_PERMS[current] : undefined
  const allowed = !currentPerm || hasPermission(currentPerm)

  const { mode, accent } = useSocTheme()
  const [chatOpen, setChatOpen] = useState(false)
  const [chatSeed, setChatSeed] = useState<string | null>(null)
  const [viewFull, setViewFull] = useState(false)
  // runtime-dynamic rail membership (mirrors production NavigationRail):
  // integrations are fetched once, orchestrator status is polled every 10s. No
  // rail item is gated today — Auto Ops is intentionally always-visible and
  // Timesketch has no redesign screen yet — but the plumbing is live so adding
  // a NavGate to data.ts is the only step needed to gate one (see data.ts).
  const [enabledIntegrations, setEnabledIntegrations] = useState<string[]>([])
  const [orchestratorEnabled, setOrchestratorEnabled] = useState(false)

  // fire desktop notifications for newly-arrived findings (gated by the General
  // `show_notifications` setting + browser permission)
  useDesktopNotifications()
  // nav rail collapsed (icons only) vs. expanded (icons + labels); sticky
  const [railExpanded, setRailExpanded] = useState<boolean>(() => {
    try {
      return localStorage.getItem('soc.rail.expanded') === '1'
    } catch {
      return false
    }
  })
  const toggleRail = useCallback(() => {
    setRailExpanded((v) => {
      const next = !v
      try {
        localStorage.setItem('soc.rail.expanded', next ? '1' : '0')
      } catch {
        /* localStorage unavailable — keep in-memory state only */
      }
      return next
    })
  }, [])

  const openChat = useCallback((prompt?: string) => {
    setChatOpen(true)
    if (prompt) setChatSeed(prompt)
  }, [])
  const closeChat = useCallback(() => setChatOpen(false), [])

  const go = useCallback(
    (next: ScreenKey) => {
      if (valid && next === current) return
      navigate(`/${next}`)
    },
    [valid, current, navigate],
  )

  // leaving a screen drops any full-bleed detail it had open; screens that
  // deep-link a detail (cases) re-assert viewFull from their own URL state.
  useEffect(() => {
    setViewFull(false)
  }, [current])

  // nav membership: integrations once, orchestrator status on a 10s poll
  useEffect(() => {
    configApi
      .getIntegrations()
      .then((res) =>
        setEnabledIntegrations((res.data as { enabled_integrations?: string[] })?.enabled_integrations || []),
      )
      .catch(() => setEnabledIntegrations([]))
    const pollStatus = () =>
      orchestratorApi
        .getStatus()
        .then((res) => setOrchestratorEnabled(Boolean((res.data as { enabled?: boolean })?.enabled)))
        .catch(() => {
          /* keep the previous value on a transient failure */
        })
    pollStatus()
    const id = setInterval(pollStatus, 10_000)
    return () => clearInterval(id)
  }, [])

  const [title, sub] = valid ? TITLES[current] : ['Page not found', 'This page doesn’t exist']
  const Screen = SCREENS[current]

  const wrapperClass = ['soc-console', chatOpen ? 'chat-active' : ''].filter(Boolean).join(' ')

  const mainClass = ['main', chatOpen ? 'chat-open' : ''].filter(Boolean).join(' ')

  return (
    <div className={wrapperClass} data-theme={mode} style={accentVars(accent.a, accent.b)}>
      <ToastProvider>
      <div className="shell">
        {/* nav rail */}
        <nav className={`rail${railExpanded ? ' expanded' : ''}`}>
          <button
            className="nav-btn nav-toggle"
            onClick={toggleRail}
            aria-label={railExpanded ? 'Collapse navigation' : 'Expand navigation'}
            aria-expanded={railExpanded}
          >
            <VigilMark className="nav-logo mark" />
            <VigilLogo className="nav-logo full" />
          </button>
          <div className="rail-sep" />
          {NAV.filter(([, , key, gate]) => {
            const perm = key ? SCREEN_PERMS[key] : undefined
            if (perm && !hasPermission(perm)) return false
            if (gate?.integration && !enabledIntegrations.includes(gate.integration)) return false
            if (gate?.orchestrator && !orchestratorEnabled) return false
            return true
          }).map((n) => {
            const [icon, label, key] = n
            const active = valid && key === current
            return (
              <button
                key={label}
                className={`nav-btn${active ? ' active' : ''}`}
                onClick={key ? () => go(key) : undefined}
                aria-label={label}
              >
                <Icon name={icon} />
                <span className="nav-label">{label}</span>
                <span className="tip">{label}</span>
              </button>
            )
          })}
          <div className="nav-spacer" />
          <UserMenu />
        </nav>

        {/* main */}
        <div className={mainClass}>
          <header className="topbar">
            <div className="title">
              <h1>{title}</h1>
              <p>{sub}</p>
            </div>
            <div className="grow" />
          </header>
          <main className="view" style={{ overflowY: viewFull ? 'hidden' : 'auto' }}>
            <div className="screen" style={viewFull ? { height: '100%' } : undefined}>
              <ErrorBoundary resetKey={valid ? current : 'notfound'}>
                {!valid ? (
                  <NotFoundScreen path={screen} onHome={() => go('dashboard')} />
                ) : !allowed ? (
                  <div className="access-denied">
                    <Icon name="lock" size={26} />
                    <h2>Access denied</h2>
                    <p>You don’t have permission to view this page{currentPerm ? ` (requires ${currentPerm})` : ''}.</p>
                    <button className="btn primary" onClick={() => go('dashboard')}>Back to Dashboard</button>
                  </div>
                ) : (
                  <Screen openChat={openChat} setViewFull={setViewFull} />
                )}
              </ErrorBoundary>
            </div>
          </main>
        </div>

        {/* Vigil chat dock */}
        <Chat open={chatOpen} onClose={closeChat} seed={chatSeed} onSeedConsumed={() => setChatSeed(null)} />
      </div>

      {/* floating Vigil assistant button — hidden while the chat dock is open
          (the dock has its own close control, so showing both is redundant) and
          while a full-bleed detail view is open (e.g. a case detail, which has
          its own "Open in Vigil" action — two Vigil buttons would be redundant) */}
      {!chatOpen && !viewFull && (
        <button className="chat-fab" title="Ask Vigil — AI assistant" onClick={() => openChat()}>
          <Icon name="brain" />
          <span>Ask Vigil</span>
        </button>
      )}
      </ToastProvider>
    </div>
  )
}
