/* ============================================================
   SOC Console — shell: nav rail, topbar, view router, Vigil chat
   dock, floating "Ask Vigil" FAB, and the theme tweaks panel.
   Ported from the design's index HTML + main.js.
   ============================================================ */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import './styles.css'
import { useAuth } from '../contexts/AuthContext'
import { orchestratorApi } from '../services/api'
import { Icon, type IconName } from './shared/icons'
import { NAV, TITLES, type ScreenKey, type NavGate } from './data/data'
import { ExtensionProvider, useExtensions } from './extensions/ExtensionProvider'
import ExtensionHost from './extensions/ExtensionHost'
import { accentVars } from './shell/accent'
import { bgVars, isDarkBase } from './shell/bg'
import Chat from './shell/Chat'
import UserMenu from './shell/UserMenu'
import ErrorBoundary from './shell/ErrorBoundary'
import { ToastProvider } from './shell/toast'
import { useDesktopNotifications } from './shell/useDesktopNotifications'
import { RedesignThemeProvider, useSocTheme } from './shell/theme'
import type { ScreenGoOptions, ScreenProps, SettingsSectionKey } from './shared/types'
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

/** Per-screen permission gate, mirroring the production ProtectedRoute routes
 *  (App.tsx). Screens absent here are ungated. In DEV_MODE the auth context
 *  grants every permission, so all items show in the preview. */
const SCREEN_PERMS: Partial<Record<ScreenKey, string>> = {
  cases: 'cases.read',
  decisions: 'ai_decisions.approve',
  settings: 'settings.read',
}

export default function SocConsole() {
  // the theme provider is the single source of truth for mode + accent + bg,
  // read here and written from the Appearance settings page; it must wrap the inner
  // shell (which both styles .soc-console and renders the settings screen).
  return (
    <RedesignThemeProvider>
      <ExtensionProvider>
        <SocConsoleInner />
      </ExtensionProvider>
    </RedesignThemeProvider>
  )
}

/** Like data.ts `NAV`, but the key is a plain string so extension screens
 *  (keys outside the built-in `ScreenKey` union) can join the rail. */
type NavItem = [IconName, string, string | null, NavGate?]

function SocConsoleInner() {
  // the active screen comes from the URL (/<screen>); the cases screen
  // additionally carries its open case in a ?case=<id> query param.
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const { screen } = useParams<{ screen?: string }>()
  const { mountPoints, enabledIntegrations, loading: extLoading } = useExtensions()

  // Merge built-in screens/nav/titles/perms with registered extensions;
  // built-ins win so an extension can't shadow a core screen.
  const { screens, navItems, titles, screenPerms } = useMemo(() => {
    const screens: Record<string, (p: ScreenProps) => JSX.Element> = { ...SCREENS }
    const titles: Record<string, [string, string]> = { ...TITLES }
    const screenPerms: Record<string, string | undefined> = { ...SCREEN_PERMS }
    const navItems: NavItem[] = [...(NAV as NavItem[])]
    const extNav: NavItem[] = []
    for (const { ext, mount } of mountPoints) {
      if (screens[mount.key]) continue
      screens[mount.key] = (p: ScreenProps) => (
        <ExtensionHost {...p} ext={ext} mount={mount} />
      )
      titles[mount.key] = [mount.title, mount.subtitle ?? '']
      if (mount.permission) screenPerms[mount.key] = mount.permission
      extNav.push([
        (mount.icon || 'brain') as IconName,
        mount.navLabel,
        mount.key,
        mount.gate?.integration ? { integration: mount.gate.integration } : undefined,
      ])
    }
    // Slot extension tabs just above the pinned Settings entry (append if absent).
    const settingsIdx = navItems.findIndex(([, , key]) => key === 'settings')
    navItems.splice(settingsIdx === -1 ? navItems.length : settingsIdx, 0, ...extNav)
    return { screens, navItems, titles, screenPerms }
  }, [mountPoints])

  // Unknown segment → 404, but while manifests load a deep-linked extension tab
  // shows a loading state instead of flashing 404. `current` falls back to
  // dashboard only so the chrome has a valid key to render.
  const valid = screen !== undefined && screen in screens
  const current: string = valid ? (screen as string) : 'dashboard'
  const resolvingExtension = !valid && screen !== undefined && extLoading
  // whether the user may view the current screen (DEV_MODE → always true)
  const currentPerm = valid ? screenPerms[current] : undefined
  const allowed = !currentPerm || hasPermission(currentPerm)

  const { accent, bg } = useSocTheme()
  const [chatOpen, setChatOpen] = useState(false)
  const [chatSeed, setChatSeed] = useState<string | null>(null)
  const [viewFull, setViewFull] = useState(false)
  // enabled integrations come from ExtensionProvider (so a connector configured
  // in Settings shows in the rail without a refresh); orchestrator polled 10s.
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
    // next is a plain string (not just ScreenKey) so extension screens can
    // navigate; options carry the query-string + replace behavior from main.
    (next: string, options?: ScreenGoOptions) => {
      const search = options?.search || ''
      if (valid && next === current && !search) return
      navigate({ pathname: `/${next}`, search }, { replace: options?.replace })
    },
    [valid, current, navigate],
  )
  const goSettings = useCallback(
    (section: SettingsSectionKey) => {
      navigate({ pathname: '/settings', search: `?section=${section}` })
    },
    [navigate],
  )

  // leaving a screen drops any full-bleed detail it had open; screens that
  // deep-link a detail (cases) re-assert viewFull from their own URL state.
  useEffect(() => {
    setViewFull(false)
  }, [current])

  // orchestrator status on a 10s poll (enabled integrations come from
  // ExtensionProvider above, not fetched here)
  useEffect(() => {
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

  const [title, sub] = valid ? titles[current] : ['Page not found', 'This page doesn’t exist']
  const Screen = screens[current]

  const wrapperClass = ['soc-console', chatOpen ? 'chat-active' : ''].filter(Boolean).join(' ')

  const mainClass = ['main', chatOpen ? 'chat-open' : ''].filter(Boolean).join(' ')

  return (
    <div
      className={wrapperClass}
      data-theme={isDarkBase(bg.base) ? 'dark' : 'light'}
      style={{ ...bgVars(bg.base), ...accentVars(accent.a, accent.b) }}
    >
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
          {navItems.filter(([, , key, gate]) => {
            const perm = key ? screenPerms[key] : undefined
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
                  resolvingExtension ? (
                    <div className="extension-host-status">
                      <Icon name="refresh" size={22} />
                      <p>Loading…</p>
                    </div>
                  ) : (
                    <NotFoundScreen path={screen} onHome={() => go('dashboard')} />
                  )
                ) : !allowed ? (
                  <div className="access-denied">
                    <Icon name="lock" size={26} />
                    <h2>Access denied</h2>
                    <p>You don’t have permission to view this page{currentPerm ? ` (requires ${currentPerm})` : ''}.</p>
                    <button className="btn primary" onClick={() => go('dashboard')}>Back to Dashboard</button>
                  </div>
                ) : (
                  <Screen openChat={openChat} go={go} goSettings={goSettings} setViewFull={setViewFull} />
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
        <button
          className="chat-fab"
          title="Ask Vigil - AI assistant"
          aria-label="Ask Vigil chat assistant"
          onClick={() => openChat()}
        >
          <Icon name="brain" />
          <span>Ask Vigil</span>
        </button>
      )}
      </ToastProvider>
    </div>
  )
}
