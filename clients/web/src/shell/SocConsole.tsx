import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import '../../../../docs/design/console/tokens/tokens.css'
import '../styles.css'
import './shell.css'
import { useAuth } from '../contexts/AuthContext'
import { approvalsApi, configApi, consoleApi, federationApi, mcpApi, orchestratorApi } from '../services/api'
import { Icon, type IconName } from '../shared/icons'
import { InfoTip } from '../shared/InfoTip'
import { PageHead } from '../shared/PageHead'
import { NAV, TITLES, type ConsoleScreenKey, type NavGate } from '../data/data'
import { ExtensionProvider, useExtensions } from '../extensions/ExtensionProvider'
import ExtensionHost from '../extensions/ExtensionHost'
import { useColorScheme } from '../contexts/ColorSchemeContext'
import CaseDrawer from './CaseDrawer'
import Chat from './Chat'
import CommandBar from './CommandBar'
import DevModeWarning from './DevModeWarning'
import UserMenu from './UserMenu'
import { HOME_PERM, landingScreen } from './landing'
import ConsoleTour, { type TourStopId } from './ConsoleTour'
import { markConsoleTourSeen, readConsoleTourSeen } from './consoleTourSeen'
import ErrorBoundary from './ErrorBoundary'
import { ToastProvider } from './toast'
import { useDesktopNotifications } from './useDesktopNotifications'
import { usePendingApprovals } from '../screens/decisions/useDecisions'
import type { ConsoleScreenGoOptions, ConsoleScreenProps, SettingsSectionKey } from '../shared/types'
import DashboardScreen from '../screens/dashboard/DashboardScreen'
import CasesScreen from '../screens/cases/CasesScreen'
import MetricsScreen from '../screens/metrics/MetricsScreen'
import AnalyticsScreen from '../screens/analytics/AnalyticsScreen'
import DecisionsScreen from '../screens/decisions/DecisionsScreen'
import WorkflowsScreen from '../screens/workflows/WorkflowsScreen'
import AutoOpsScreen from '../screens/autoops/AutoOpsScreen'
import HealthScreen from '../screens/health/HealthScreen'
import HomeScreen from '../screens/home/HomeScreen'
import SettingsScreen from '../screens/settings/SettingsScreen'
import NotFoundScreen from '../screens/notfound/NotFoundScreen'
import OverviewScreen from '../screens/overview/OverviewScreen'
import TriageScreen from '../screens/triage/TriageScreen'
import { VigilLogo } from '../shared/VigilLogo'
import {
  foldStatus,
  type FederationRead,
  type HealthRead,
  type McpRead,
  type RoutabilityRead,
  type StatusFold,
} from './statusLine'

const PRIMARY_KEYS = ['home', 'overview', 'triage', 'cases', 'workflows', 'settings']
// a rule is drawn only between two groups that both have a visible item
const NAV_GROUPS = [['home'], ['overview'], ['triage', 'cases'], ['workflows', 'settings']]
const MORE_KEYS = ['dashboard', 'metrics', 'analytics', 'decisions', 'autoops', 'health']

const AUTONOMY_ACT = 'Act · reversible changes on its own'
const AUTONOMY_ASSIST = 'Assist · asks before changes'

const SCREENS: Record<ConsoleScreenKey, (props: ConsoleScreenProps) => JSX.Element> = {
  overview: OverviewScreen,
  triage: TriageScreen,
  dashboard: DashboardScreen,
  home: HomeScreen,
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
  home: HOME_PERM,
  settings: 'settings.read',
}

const CHAT_WIDTH = 400

const setupTip = (left: number) =>
  `Finish setting up Vigil: ${left === 1 ? '1 step is' : `${left} steps are`} left. Opens the setup guide.`

export default function SocConsole() {
  return (
    <ExtensionProvider>
      <SocConsoleInner />
    </ExtensionProvider>
  )
}

/** key is a plain string, so extension screens can join the nav row */
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
  const landing = landingScreen(hasPermission)
  const landingLabel = landing === 'home' ? 'Home' : 'Overview'
  const current: string = valid ? (screen as string) : landing
  const resolvingExtension = !valid && screen !== undefined && extLoading
  const currentPerm = valid ? screenPerms[current] : undefined
  const allowed = !currentPerm || hasPermission(currentPerm)

  const { scheme } = useColorScheme()
  const [chatOpen, setChatOpen] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const [assist, setAssist] = useState<boolean | null>(null)
  const [status, setStatus] = useState<StatusFold | null>(null)
  // open setup steps; null when unread or failed, so no chip and no menu count
  const [setupLeft, setSetupLeft] = useState<number | null>(null)
  const moreRef = useRef<HTMLDivElement>(null)
  const [viewportWidth, setViewportWidth] = useState(() =>
    typeof window === 'undefined' ? 1440 : window.innerWidth,
  )
  const [chatSeed, setChatSeed] = useState<string | null>(null)
  const [caseSeed, setCaseSeed] = useState<string | null>(null)
  const [drawerCase, setDrawerCase] = useState<string | null>(null)
  const [fill, setFill] = useState<{ text: string; seq: number } | null>(null)
  const fillCommand = useCallback((text: string) => setFill((prev) => ({ text, seq: (prev?.seq ?? 0) + 1 })), [])
  const [viewFull, setViewFull] = useState(false)
  const [wallMode, setWallMode] = useState(false)
  const homePerm = SCREEN_PERMS.home
  const canTourHome = !homePerm || hasPermission(homePerm)
  const tourStops = useMemo<readonly TourStopId[]>(
    () => (canTourHome ? ['nav', 'attention', 'ask'] : ['nav', 'ask']),
    [canTourHome],
  )
  const [tourOn, setTourOn] = useState(() => !readConsoleTourSeen())
  const [tourIndex, setTourIndex] = useState(0)
  // from ExtensionProvider, so a connector configured in Settings reaches the
  // nav row without a refresh
  const [orchestratorEnabled, setOrchestratorEnabled] = useState(false)
  const [demoOn, setDemoOn] = useState(false)

  useDesktopNotifications()
  // the nav row is on screen from every other view; without this
  // badge a parked run sat in a tab nobody opened
  const parked = usePendingApprovals().actions.length
  // needs-you is uncapped; the decisions badge stays on the pending list
  const [needsYou, setNeedsYou] = useState(0)
  const canReadRoutability = hasPermission('settings.write')

  const openChat = useCallback((prompt?: string) => {
    setChatOpen(true)
    if (prompt) setChatSeed(prompt)
  }, [])
  const closeChat = useCallback(() => setChatOpen(false), [])
  // the open case: the drawer wins over the full page's ?case= param
  const pageCase = current === 'cases' && allowed ? new URLSearchParams(location.search).get('case') : null
  const openCaseId = drawerCase ?? pageCase
  // with a case open the text goes to its own composer, not the dock
  const askVigil = useCallback((text?: string) => {
    if (openCaseId && text) setCaseSeed(text)
    else openChat(text)
  }, [openCaseId, openChat])
  const clearCaseSeed = useCallback(() => setCaseSeed(null), [])

  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
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

  // The nav is unmounted in wall mode, and Ask Vigil is unmounted while the
  // dock or a full-bleed view is open. Mount the target before that stop.
  const prepareStop = useCallback((index: number) => {
    const stop = tourStops[Math.min(index, Math.max(tourStops.length - 1, 0))]
    if (stop === 'attention') go('home')
    if (stop === 'nav' || stop === 'ask') setWallMode(false)
    if (stop === 'ask') {
      setChatOpen(false)
      setViewFull(false)
    }
  }, [tourStops, go])

  const showStop = useCallback((index: number) => {
    if (index < 0 || index >= tourStops.length) return
    prepareStop(index)
    setTourIndex(index)
  }, [prepareStop, tourStops])

  const startTour = useCallback(() => {
    setChatOpen(false)
    setViewFull(false)
    setWallMode(false)
    setTourIndex(0)
    setTourOn(true)
  }, [])

  const dismissTour = useCallback(() => {
    markConsoleTourSeen()
    setTourOn(false)
  }, [])

  useEffect(() => {
    if (!tourOn) return
    prepareStop(tourIndex)
  }, [tourOn, tourIndex, prepareStop, wallMode, chatOpen, viewFull])

  // screens that deep-link a detail re-assert viewFull from their own URL state; a child's effect
  // runs before this one on first mount, so only a change of screen may clear what it set
  const shownScreen = useRef(current)
  useEffect(() => {
    if (shownScreen.current === current) return
    shownScreen.current = current
    setViewFull(false)
    setWallMode(false)
  }, [current])

  useEffect(() => {
    let live = true
    const pollNeedsYou = () => {
      approvalsApi
        .needsYou()
        .then((res) => {
          if (live) setNeedsYou(res.data.count)
        })
        .catch(() => {
          /* keep the previous count */
        })
    }
    pollNeedsYou()
    const id = setInterval(pollNeedsYou, 20_000)
    return () => {
      live = false
      clearInterval(id)
    }
  }, [])

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

  // re-read on each screen change so finishing a step shows without a reload
  useEffect(() => {
    let live = true
    configApi
      .getSetupSteps()
      .then((res) => {
        if (live) setSetupLeft(res.data.steps.filter((step) => !step.done).length)
      })
      .catch(() => {
        if (live) setSetupLeft(null)
      })
    return () => {
      live = false
    }
  }, [current])

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
    let inFlight = false
    const settled = <T,>(p: Promise<T>): Promise<T | null> => p.then((v) => v).catch(() => null)
    // the last fold stays up while a round runs; a failed read folds as null
    const pollStatus = () => {
      if (inFlight) return
      inFlight = true
      Promise.all([
        settled(consoleApi.getHealth().then((res) => res.data as HealthRead)),
        settled(federationApi.getHealth().then((res) => res.data as FederationRead)),
        settled(mcpApi.getStatuses().then((res) => res.data as McpRead)),
        canReadRoutability
          ? settled(consoleApi.getRoutability().then((res) => res.data as RoutabilityRead))
          : Promise.resolve(null),
      ]).then(([health, federation, mcp, routability]) => {
        inFlight = false
        if (live) setStatus(foldStatus({ health, federation, mcp, routability }))
      })
    }
    pollStatus()
    const id = setInterval(pollStatus, 30_000)
    return () => {
      live = false
      clearInterval(id)
    }
  }, [canReadRoutability])

  useEffect(() => {
    if (!moreOpen) return
    const onDoc = (e: MouseEvent) => {
      if (!moreRef.current?.contains(e.target as Node)) setMoreOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMoreOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [moreOpen])

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
  const navGroups = NAV_GROUPS.map((keys) =>
    keys.map((key) => byKey.get(key)).filter((item): item is NavItem => Boolean(item)),
  ).filter((group) => group.length > 0)

  const navButton = (item: NavItem) => {
    const [icon, label, key] = item
    if (!key) return null
    const count = key === 'decisions' ? parked : key === 'home' || key === 'cases' ? needsYou : 0
    const active = valid && key === current
    return (
      <button
        key={key}
        type="button"
        className={`vg-nav-btn${active ? ' active' : ''}`}
        aria-current={active ? 'page' : undefined}
        aria-label={count ? `${label} (${count} waiting)` : label}
        onClick={() => {
          setMoreOpen(false)
          go(key, key === 'decisions' && parked > 0 ? { search: '?tab=approvals' } : undefined)
        }}
      >
        <Icon name={icon} size={14} />
        <span>{label}</span>
        {count > 0 && (
          <span className={`vg-nav-count${key === 'home' ? '' : ' quiet'}`}>{count > 99 ? '99+' : count}</span>
        )}
      </button>
    )
  }

  const wrapperClass = [
    'soc-console',
    scheme === 'light' ? 'vg-light' : 'vg-dark',
    chatOpen ? 'chat-active' : '',
  ].filter(Boolean).join(' ')

  // these draw their own heading; Home's headline is its heading
  const ownsHeading = ['home', 'overview', 'triage', 'cases', 'workflows', 'settings'].includes(current)
  const showHeading = valid && allowed && !ownsHeading && !wallMode && !viewFull
  const mainClass = ['main', chatOpen ? 'chat-open' : ''].filter(Boolean).join(' ')
  const effectiveChatWidth = viewportWidth <= 600 ? viewportWidth : CHAT_WIDTH
  const consoleStyle = { '--chat-w': `${effectiveChatWidth}px` } as CSSProperties

  return (
    <div
      className={wrapperClass}
      data-theme={scheme}
      style={consoleStyle}
    >
      <ToastProvider>
      <div className="shell vg-shell">
        {!wallMode && <header className="vg-header">
          <div className="vg-brand">
            <VigilLogo className="vg-logo" />
            <DevModeWarning />
          </div>
          <CommandBar
            boards={[...primary, ...more].map((item) => {
              const key = item[2] as string
              return { key, label: item[1] }
            })}
            onOpenChat={askVigil}
            caseOpen={openCaseId !== null}
            onOpenCase={setDrawerCase}
            onGo={(next, options) => {
              setDrawerCase(null) // the drawer would sit over the next screen
              go(next, options)
            }}
            fill={fill}
          />
          <div className="vg-header-end">
            {assist !== null && (
              <div className="vg-autonomy">
                <button
                  type="button"
                  className="vg-autonomy-link"
                  aria-label={`Autonomy · ${assist ? AUTONOMY_ASSIST : AUTONOMY_ACT}`}
                  onClick={() => goSettings('autoinvestigate')}
                >
                  <span className="vg-autonomy-label">Autonomy</span>
                  {assist ? AUTONOMY_ASSIST : AUTONOMY_ACT}
                </button>
                <InfoTip
                  label="How autonomy is derived"
                  source="force_manual_approval and auto_response_enabled."
                  calculation="Assist when the first is set or the second is off; otherwise Act."
                  limit="Both are set in Settings › Limits & autonomy."
                />
              </div>
            )}
            <UserMenu onShowTour={startTour} setupLeft={setupLeft} />
          </div>
        </header>}
        {!wallMode && <nav className="vg-nav" aria-label="Primary">
          {navGroups.map((group, i) => (
            <Fragment key={group[0][2]}>
              {i > 0 && <span className="vg-nav-sep" aria-hidden="true" />}
              {group.map(navButton)}
            </Fragment>
          ))}
          {more.length > 0 && (
            <>
              {navGroups.length > 0 && <span className="vg-nav-sep" aria-hidden="true" />}
              <div className="vg-more" ref={moreRef}>
                <button
                  type="button"
                  className={`vg-nav-btn${moreOpen || moreCurrent ? ' active' : ''}`}
                  aria-haspopup="menu"
                  aria-expanded={moreOpen}
                  aria-label="More"
                  onClick={() => setMoreOpen((open) => !open)}
                >
                  <Icon name="more" size={14} />
                  <span>More</span>
                </button>
                {moreOpen && (
                  <div className="vg-more-menu" role="menu" aria-label="More screens">
                    {more.map(navButton)}
                  </div>
                )}
              </div>
            </>
          )}
          <div className="vg-nav-end">
            {status && (
              <div
                className={`vg-status-chip${status.level === 'poor' ? ' is-poor' : ''}`}
                role="status"
                aria-label="System status"
                data-level={status.level}
                title={status.sentence}
              >
                <span className="vg-status-dot" aria-hidden="true" />
                <span className="vg-status-text">{status.sentence}</span>
              </div>
            )}
            {setupLeft !== null && setupLeft > 0 && (
              <Link
                to="/setup"
                className="vg-setup-chip"
                title={setupTip(setupLeft)}
                aria-label={setupTip(setupLeft)}
              >
                <Icon name="check" size={13} />
                Finish setup
                <span className="vg-setup-count">{setupLeft} left</span>
              </Link>
            )}
          </div>
        </nav>}

        {/* main */}
        <div className={mainClass}>
          {demoOn && (
            <div className="demo-banner" role="status">
              The data on screen is demo data.
            </div>
          )}
          <main className="view" style={{ overflowY: viewFull ? 'hidden' : 'auto' }}>
            <div className="screen" style={viewFull ? { height: '100%' } : undefined}>
              {showHeading && (
                <div className="vg-page-head">
                  <PageHead title={title} description={sub} level="h1" />
                </div>
              )}
              <ErrorBoundary resetKey={valid ? current : 'notfound'}>
                {!valid ? (
                  resolvingExtension ? (
                    <div className="extension-host-status">
                      <Icon name="refresh" size={22} />
                      <p>Loading…</p>
                    </div>
                  ) : (
                    <NotFoundScreen path={screen} homeLabel={landingLabel} onHome={() => go(landing)} />
                  )
                ) : !allowed ? (
                  <div className="access-denied">
                    <Icon name="lock" size={26} />
                    <h2>Access denied</h2>
                    <p>You don’t have permission to view this page{currentPerm ? ` (requires ${currentPerm})` : ''}.</p>
                    <button className="btn primary" onClick={() => go(landing)}>Back to {landingLabel}</button>
                  </div>
                ) : (
                  <Screen openChat={openChat} go={go} goSettings={goSettings} openCase={setDrawerCase} setViewFull={setViewFull} setWallMode={setWallMode} caseSeed={drawerCase ? null : caseSeed} onCaseSeedConsumed={clearCaseSeed} startTour={startTour} fillCommand={wallMode ? undefined : fillCommand} chatOpen={chatOpen} />
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
          pageKey={current}
          pageTitle={title}
          onSeedConsumed={() => setChatSeed(null)}
        />
        {drawerCase && (
          <CaseDrawer
            caseId={drawerCase}
            onClose={() => setDrawerCase(null)}
            pageKey={current}
            seed={caseSeed}
            onSeedConsumed={clearCaseSeed}
          />
        )}
      </div>

      {/* floating Vigil assistant button — hidden while the chat dock is open
          (the dock has its own close control, so showing both is redundant) and
          while a full-bleed detail view is open (a case detail pins its own
          Ask composer, so a second Vigil button would be redundant) */}
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
      {tourOn && (
        <ConsoleTour
          stops={tourStops}
          index={tourIndex}
          onIndex={showStop}
          onDismiss={dismissTour}
        />
      )}
      </ToastProvider>
    </div>
  )
}
