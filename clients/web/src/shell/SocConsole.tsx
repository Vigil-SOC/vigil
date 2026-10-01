import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import '../../../../docs/design/console/tokens/tokens.css'
import '../styles.css'
import './shell.css'
import { useAuth } from '../contexts/AuthContext'
import { configApi, consoleApi, federationApi, mcpApi, orchestratorApi } from '../services/api'
import { Icon, type IconName } from '../shared/icons'
import { NAV, TITLES, type ConsoleScreenKey, type NavGate } from '../data/data'
import { ExtensionProvider, useExtensions } from '../extensions/ExtensionProvider'
import ExtensionHost from '../extensions/ExtensionHost'
import { accentVars } from '../shared/accent'
import { bgVars, isDarkBase } from './bg'
import Chat from './Chat'
import DevModeWarning from './DevModeWarning'
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
import HealthScreen from '../screens/health/HealthScreen'
import SettingsScreen from '../screens/settings/SettingsScreen'
import NotFoundScreen from '../screens/notfound/NotFoundScreen'
import { VigilLogo } from '../shared/VigilLogo'
import {
  foldStatus,
  type FederationRead,
  type HealthRead,
  type McpRead,
  type RoutabilityRead,
  type StatusFold,
} from './statusLine'

const PRIMARY_KEYS = ['cases', 'workflows', 'settings']
const MORE_KEYS = ['dashboard', 'metrics', 'analytics', 'decisions', 'autoops', 'health']

const AUTONOMY_ACT = 'Autonomy · Act · reversible changes on its own'
const AUTONOMY_ASSIST = 'Autonomy · Assist · asks before changes'

const LEVEL_WORD: Record<StatusFold['level'], string> = {
  good: 'Good',
  fair: 'Fair',
  poor: 'Poor',
}

const SCREENS: Record<ConsoleScreenKey, (props: ConsoleScreenProps) => JSX.Element> = {
  dashboard: DashboardScreen,
  cases: CasesScreen,
  metrics: MetricsScreen,
  analytics: AnalyticsScreen,
  decisions: DecisionsScreen,
  workflows: WorkflowsScreen,
  autoops: AutoOpsScreen,
  health: HealthScreen,
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

  const { accent, bg, scheme } = useSocTheme()
  const [chatOpen, setChatOpen] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const [infoOpen, setInfoOpen] = useState(false)
  const [assist, setAssist] = useState<boolean | null>(null)
  const [status, setStatus] = useState<StatusFold | null>(null)
  const moreRef = useRef<HTMLDivElement>(null)
  const infoRef = useRef<HTMLDivElement>(null)
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
  const [demoOn, setDemoOn] = useState(false)

  useDesktopNotifications()
  // the rail is the only thing on screen from every other view; without this
  // badge a parked run sat in a tab nobody opened
  const parked = usePendingApprovals().actions.length
  const canReadRoutability = hasPermission('settings.write')

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

  useEffect(() => {
    let live = true
    configApi
      .getDemoMode()
      .then((res) => {
        if (live) setDemoOn(Boolean(res.data?.enabled))
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    let live = true
    configApi
      .getAutonomy()
      .then((res) => {
        if (!live) return
        const auto = Boolean(res.data?.auto_response_enabled)
        const force = Boolean(res.data?.force_manual_approval)
        setAssist(force || !auto)
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    let live = true
    const settled = <T,>(p: Promise<T>): Promise<T | null> => p.then((v) => v).catch(() => null)
    Promise.all([
      settled(consoleApi.getHealth().then((res) => res.data as HealthRead)),
      settled(federationApi.getHealth().then((res) => res.data as FederationRead)),
      settled(mcpApi.getStatuses().then((res) => res.data as McpRead)),
      canReadRoutability
        ? settled(consoleApi.getRoutability().then((res) => res.data as RoutabilityRead))
        : Promise.resolve(null),
    ]).then(([health, federation, mcp, routability]) => {
      if (live) setStatus(foldStatus({ health, federation, mcp, routability }))
    })
    return () => {
      live = false
    }
  }, [canReadRoutability])

  useEffect(() => {
    if (!moreOpen && !infoOpen) return
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node
      if (moreOpen && !moreRef.current?.contains(t)) setMoreOpen(false)
      if (infoOpen && !infoRef.current?.contains(t)) setInfoOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setMoreOpen(false)
        setInfoOpen(false)
      }
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [moreOpen, infoOpen])

  const [title, sub] = valid ? titles[current] : ['Page not found', 'This page doesn’t exist']
  const Screen = screens[current]

  const visibleNav = navItems.filter(([, , key, gate]) => {
    const perm = key ? screenPerms[key] : undefined
    if (perm && !hasPermission(perm)) return false
    if (gate?.integration && !enabledIntegrations.includes(gate.integration)) return false
    if (gate?.orchestrator && !orchestratorEnabled) return false
    return Boolean(key)
  })
  const byKey = new Map(visibleNav.map((item) => [item[2] as string, item]))
  const primary = PRIMARY_KEYS.map((key) => byKey.get(key)).filter((item): item is NavItem => Boolean(item))
  const moreKeySet = new Set<string>(MORE_KEYS)
  const primaryKeySet = new Set<string>(PRIMARY_KEYS)
  const more = [
    ...MORE_KEYS.map((key) => byKey.get(key)).filter((item): item is NavItem => Boolean(item)),
    ...visibleNav.filter((item) => {
      const key = item[2] as string
      return !primaryKeySet.has(key) && !moreKeySet.has(key)
    }),
  ]
  const moreCurrent = more.some((item) => valid && item[2] === current)

  const navButton = (item: NavItem) => {
    const [icon, rawLabel, key] = item
    if (!key) return null
    const label = key === 'workflows' ? 'Agents & workflows' : rawLabel
    const waiting = key === 'decisions' ? parked : 0
    const active = valid && key === current
    return (
      <button
        key={key}
        type="button"
        className={`vg-nav-btn${active ? ' active' : ''}`}
        aria-current={active ? 'page' : undefined}
        aria-label={waiting ? `${label} (${waiting} waiting)` : label}
        onClick={() => {
          setMoreOpen(false)
          go(key, waiting ? { search: '?tab=approvals' } : undefined)
        }}
      >
        <Icon name={icon} size={16} />
        <span>{label}</span>
        {waiting > 0 && <span className="vg-nav-count">{waiting > 99 ? '99+' : waiting}</span>}
      </button>
    )
  }

  const wrapperClass = [
    'soc-console',
    scheme === 'light' ? 'vg-light' : 'vg-dark',
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
      <div className="shell vg-shell">
        <header className="vg-header">
          <div className="vg-brand">
            <VigilLogo className="vg-logo" />
            <DevModeWarning />
          </div>
          <div className="vg-command-slot" data-command-slot="" aria-hidden="true" />
          <div className="vg-header-end">
            {assist !== null && (
              <div className="vg-autonomy" ref={infoRef}>
                <span>{assist ? AUTONOMY_ASSIST : AUTONOMY_ACT}</span>
                <button
                  type="button"
                  className="vg-info"
                  aria-label="How autonomy is derived"
                  aria-expanded={infoOpen}
                  onClick={() => setInfoOpen((open) => !open)}
                >
                  <Icon name="info" size={14} />
                </button>
                {infoOpen && (
                  <div className="vg-info-pop" role="tooltip">
                    Assist when force_manual_approval is set or auto_response_enabled is off; otherwise Act.
                  </div>
                )}
              </div>
            )}
            <UserMenu />
          </div>
        </header>
        <nav className="vg-nav" aria-label="Primary">
          {primary.map(navButton)}
          {more.length > 0 && (
            <div className="vg-more" ref={moreRef}>
              <button
                type="button"
                className={`vg-nav-btn${moreOpen || moreCurrent ? ' active' : ''}`}
                aria-haspopup="menu"
                aria-expanded={moreOpen}
                aria-label="More"
                onClick={() => setMoreOpen((open) => !open)}
              >
                <Icon name="more" size={16} />
                <span>More</span>
              </button>
              {moreOpen && (
                <div className="vg-more-menu" role="menu" aria-label="More screens">
                  {more.map(navButton)}
                </div>
              )}
            </div>
          )}
        </nav>
        <div
          className={`vg-status${status?.level === 'poor' ? ' is-poor' : ''}`}
          role={status ? 'status' : undefined}
          aria-label={status ? 'System status' : undefined}
          data-level={status?.level}
        >
          {status && (
            <>
              <span className="vg-status-level">{LEVEL_WORD[status.level]}</span>
              <span>{status.sentence}</span>
            </>
          )}
        </div>

        {/* main */}
        <div className={mainClass}>
          <header className="topbar">
            <div className="title">
              <h1>{title}</h1>
              <p>{sub}</p>
            </div>
            <div className="grow" />
          </header>
          {demoOn && (
            <div className="demo-banner" role="status">
              The data on screen is demo data.
            </div>
          )}
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
