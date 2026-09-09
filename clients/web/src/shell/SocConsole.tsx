import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import '../styles.css'
import { useAuth } from '../contexts/AuthContext'
import { orchestratorApi } from '../services/api'
import { Icon, type IconName } from '../shared/icons'
import { NAV, TITLES, type ConsoleScreenKey, type NavGate } from '../data/data'
import { ExtensionProvider, useExtensions } from '../extensions/ExtensionProvider'
import ExtensionHost from '../extensions/ExtensionHost'
import { accentVars } from '../shared/accent'
import { bgVars, isDarkBase } from './bg'
import Chat from './Chat'
import UserMenu from './UserMenu'
import ErrorBoundary from './ErrorBoundary'
import { ToastProvider } from './toast'
import { useDesktopNotifications } from './useDesktopNotifications'
import { usePendingApprovals } from '../screens/decisions/useDecisions'
import { SocThemeProvider, useSocTheme } from './theme'
import type { ConsoleScreenGoOptions, ConsoleScreenProps, SettingsSectionKey } from '../shared/types'
import DashboardScreen from '../screens/dashboard/DashboardScreen'
import CasesScreen from '../screens/cases/CasesScreen'
import MetricsScreen from '../screens/metrics/MetricsScreen'
import AnalyticsScreen from '../screens/analytics/AnalyticsScreen'
import DecisionsScreen from '../screens/decisions/DecisionsScreen'
import WorkflowsScreen from '../screens/workflows/WorkflowsScreen'
import AutoOpsScreen from '../screens/autoops/AutoOpsScreen'
import SettingsScreen from '../screens/settings/SettingsScreen'
import NotFoundScreen from '../screens/notfound/NotFoundScreen'
import { VigilMark, VigilLogo } from '../shared/VigilLogo'

const SCREENS: Record<ConsoleScreenKey, (props: ConsoleScreenProps) => JSX.Element> = {
  dashboard: DashboardScreen,
  cases: CasesScreen,
  metrics: MetricsScreen,
  analytics: AnalyticsScreen,
  decisions: DecisionsScreen,
  workflows: WorkflowsScreen,
  autoops: AutoOpsScreen,
  settings: SettingsScreen,
}

/** The only permission check in the app; ProtectedRoute handles auth alone.
 *  Screens absent here are ungated, and DEV_MODE grants everything. */
const SCREEN_PERMS: Partial<Record<ConsoleScreenKey, string>> = {
  cases: 'cases.read',
  decisions: 'ai_decisions.approve',
  settings: 'settings.read',
}

const CHAT_MIN_WIDTH = 360
const CHAT_MAX_WIDTH = 720
const CHAT_DEFAULT_WIDTH = 420
const CHAT_WIDTH_STORAGE_KEY = 'soc.chat.width.v1'

function clampChatPreference(width: number): number {
  return Math.min(CHAT_MAX_WIDTH, Math.max(CHAT_MIN_WIDTH, Math.round(width)))
}

/** never past half the screen, so the main canvas stays usable */
function chatMaxForViewport(viewportWidth: number): number {
  return Math.min(
    CHAT_MAX_WIDTH,
    Math.max(CHAT_MIN_WIDTH, Math.floor(viewportWidth * 0.5)),
  )
}

function readChatWidth(): number {
  try {
    const raw = localStorage.getItem(CHAT_WIDTH_STORAGE_KEY)
    if (raw) {
      const parsed = Number.parseInt(raw, 10)
      if (Number.isFinite(parsed)) return clampChatPreference(parsed)
    }
  } catch {
    /* empty */
  }
  return CHAT_DEFAULT_WIDTH
}

export default function SocConsole() {
  // the theme provider must wrap the inner shell: that shell both styles
  // .soc-console and renders the Appearance page that writes to it
  return (
    <SocThemeProvider>
      <ExtensionProvider>
        <SocConsoleInner />
      </ExtensionProvider>
    </SocThemeProvider>
  )
}

/** key is a plain string, so extension screens can join the rail */
type NavItem = [IconName, string, string | null, NavGate?]

function SocConsoleInner() {
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const { screen } = useParams<{ screen?: string }>()
  const location = useLocation()
  const { mountPoints, enabledIntegrations, loading: extLoading } = useExtensions()

  // built-ins win, so an extension can't shadow a core screen
  const { screens, navItems, titles, screenPerms } = useMemo(() => {
    const screens: Record<string, (p: ConsoleScreenProps) => JSX.Element> = { ...SCREENS }
    const titles: Record<string, [string, string]> = { ...TITLES }
    const screenPerms: Record<string, string | undefined> = { ...SCREEN_PERMS }
    const navItems: NavItem[] = [...(NAV as NavItem[])]
    const extNav: NavItem[] = []
    for (const { ext, mount } of mountPoints) {
      if (screens[mount.key]) continue
      screens[mount.key] = (p: ConsoleScreenProps) => (
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
    // extension tabs slot above the pinned Settings entry
    const settingsIdx = navItems.findIndex(([, , key]) => key === 'settings')
    navItems.splice(settingsIdx === -1 ? navItems.length : settingsIdx, 0, ...extNav)
    return { screens, navItems, titles, screenPerms }
  }, [mountPoints])

  // while manifests load, a deep-linked extension tab shows loading rather than
  // flashing 404
  const valid = screen !== undefined && screen in screens
  const current: string = valid ? (screen as string) : 'dashboard'
  const resolvingExtension = !valid && screen !== undefined && extLoading
  const currentPerm = valid ? screenPerms[current] : undefined
  const allowed = !currentPerm || hasPermission(currentPerm)

  const { accent, bg } = useSocTheme()
  const [chatOpen, setChatOpen] = useState(false)
  const [chatWidth, setChatWidth] = useState(readChatWidth)
  const [viewportWidth, setViewportWidth] = useState(() =>
    typeof window === 'undefined' ? 1440 : window.innerWidth,
  )
  const [chatResizing, setChatResizing] = useState(false)
  const [chatSeed, setChatSeed] = useState<string | null>(null)
  const [viewFull, setViewFull] = useState(false)
  // from ExtensionProvider, so a connector configured in Settings reaches the
  // rail without a refresh
  const [orchestratorEnabled, setOrchestratorEnabled] = useState(false)

  useDesktopNotifications()
  // the rail is the only thing on screen from every other view; without this
  // badge a parked run sat in a tab nobody opened
  const parked = usePendingApprovals().actions.length
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
        /* empty */
      }
      return next
    })
  }, [])

  const openChat = useCallback((prompt?: string) => {
    setChatOpen(true)
    if (prompt) setChatSeed(prompt)
  }, [])
  const closeChat = useCallback(() => setChatOpen(false), [])

  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const previewChatWidth = useCallback((width: number) => {
    setChatWidth(clampChatPreference(width))
  }, [])
  const commitChatWidth = useCallback((width: number) => {
    const next = clampChatPreference(width)
    setChatWidth(next)
    try {
      localStorage.setItem(CHAT_WIDTH_STORAGE_KEY, String(next))
    } catch {
      /* empty */
    }
  }, [])

  const go = useCallback(
    (next: string, options?: ConsoleScreenGoOptions) => {
      const search = options?.search || ''
      // Compare the query too, not just the screen: a repeat badged click is a
      // no-op that used to push a duplicate history entry, and an unbadged
      // click from ?tab=approvals is a real move that used to be swallowed.
      if (valid && next === current && search === location.search) return
      navigate({ pathname: `/${next}`, search }, { replace: options?.replace })
    },
    [valid, current, navigate, location.search],
  )
  const goSettings = useCallback(
    (section: SettingsSectionKey) => {
      navigate({ pathname: '/settings', search: `?section=${section}` })
    },
    [navigate],
  )

  // screens that deep-link a detail re-assert viewFull from their own URL state
  useEffect(() => {
    setViewFull(false)
  }, [current])

  useEffect(() => {
    const pollStatus = () =>
      orchestratorApi
        .getStatus()
        .then((res) => setOrchestratorEnabled(Boolean((res.data as { enabled?: boolean })?.enabled)))
        .catch(() => {
          /* keep the previous value */
        })
    pollStatus()
    const id = setInterval(pollStatus, 10_000)
    return () => clearInterval(id)
  }, [])

  const [title, sub] = valid ? titles[current] : ['Page not found', 'This page doesn’t exist']
  const Screen = screens[current]

  const wrapperClass = [
    'soc-console',
    chatOpen ? 'chat-active' : '',
    chatResizing ? 'chat-resizing' : '',
  ].filter(Boolean).join(' ')

  const mainClass = ['main', chatOpen ? 'chat-open' : ''].filter(Boolean).join(' ')
  const chatViewportMax = chatMaxForViewport(viewportWidth)
  const effectiveChatWidth = viewportWidth <= 600
    ? viewportWidth
    : Math.min(chatWidth, chatViewportMax)
  const resizeMinWidth = viewportWidth <= 600 ? effectiveChatWidth : CHAT_MIN_WIDTH
  const resizeMaxWidth = viewportWidth <= 600 ? effectiveChatWidth : chatViewportMax
  const consoleStyle = {
    ...bgVars(bg.base),
    ...accentVars(accent.a, accent.b),
    '--chat-w': `${effectiveChatWidth}px`,
  } as CSSProperties

  return (
    <div
      className={wrapperClass}
      data-theme={isDarkBase(bg.base) ? 'dark' : 'light'}
      style={consoleStyle}
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
            const waiting = key === 'decisions' ? parked : 0
            return (
              <button
                key={label}
                className={`nav-btn${active ? ' active' : ''}`}
                // a badged item is a pointer at the approvals queue, so send the
                // click there rather than to the screen's default tab (#746)
                onClick={key ? () => go(key, waiting ? { search: '?tab=approvals' } : undefined) : undefined}
                aria-label={waiting ? `${label} (${waiting} waiting)` : label}
              >
                <Icon name={icon} />
                <span className="nav-label">{label}</span>
                {waiting > 0 && <span className="nav-count">{waiting > 99 ? '99+' : waiting}</span>}
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
        <Chat
          open={chatOpen}
          onClose={closeChat}
          seed={chatSeed}
          width={effectiveChatWidth}
          minWidth={resizeMinWidth}
          maxWidth={resizeMaxWidth}
          onWidthChange={previewChatWidth}
          onWidthCommit={commitChatWidth}
          onResizeStateChange={setChatResizing}
          onSeedConsumed={() => setChatSeed(null)}
        />
      </div>

      {/* floating Vigil assistant button — hidden while the chat dock is open
          (the dock has its own close control, so showing both is redundant) and
          while a full-bleed detail view is open (e.g. a case detail, which has
          its own "Open in Vigil" action — two Vigil buttons would be redundant) */}
      {!chatOpen && !viewFull && current !== 'dashboard' && (
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
