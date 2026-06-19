/* ============================================================
   SOC Console — shell: nav rail, topbar, view router, Vigil chat
   dock, floating "Ask Vigil" FAB, and the theme tweaks panel.
   Ported from the design's index HTML + main.js.
   ============================================================ */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import './styles.css'
import { Icon } from './shared/icons'
import { NAV, TITLES, type ScreenKey } from './data/data'
import { ACCENTS, accentVars, lighten, normHex } from './shell/accent'
import Chat from './shell/Chat'
import Tweaks, { type Columns, type Density, type InsightsMode } from './shell/Tweaks'
import ErrorBoundary from './shell/ErrorBoundary'
import type { ScreenProps } from './shared/types'
import DashboardScreen from './screens/dashboard/DashboardScreen'
import CasesScreen from './screens/cases/CasesScreen'
import MetricsScreen from './screens/metrics/MetricsScreen'
import AnalyticsScreen from './screens/analytics/AnalyticsScreen'
import DecisionsScreen from './screens/decisions/DecisionsScreen'
import WorkflowsScreen from './screens/workflows/WorkflowsScreen'
import SettingsScreen from './screens/settings/SettingsScreen'
import NotFoundScreen from './screens/notfound/NotFoundScreen'
import vigilMark from './assets/vigil-mark.png'
import vigilLogo from './assets/vigil-logo.png'

const SCREENS: Record<ScreenKey, (props: ScreenProps) => JSX.Element> = {
  dashboard: DashboardScreen,
  cases: CasesScreen,
  metrics: MetricsScreen,
  analytics: AnalyticsScreen,
  decisions: DecisionsScreen,
  workflows: WorkflowsScreen,
  settings: SettingsScreen,
}

const SCREEN_KEYS = Object.keys(SCREENS) as ScreenKey[]
const isScreenKey = (s: string | undefined): s is ScreenKey =>
  s !== undefined && (SCREEN_KEYS as string[]).includes(s)

interface AccentState {
  key: string | null
  a: string
  b: string
}

export default function SocConsole() {
  // the active screen comes from the URL (/redesign/<screen>); the cases
  // screen additionally carries its open case in a ?case=<id> query param.
  const navigate = useNavigate()
  const { screen } = useParams<{ screen?: string }>()
  // an unknown segment (e.g. /redesign/foo) renders the 404 screen; `current`
  // falls back to dashboard only so the chrome has a valid key to render.
  const valid = isScreenKey(screen)
  const current: ScreenKey = valid ? screen : 'dashboard'

  const [chatOpen, setChatOpen] = useState(false)
  const [chatSeed, setChatSeed] = useState<string | null>(null)
  const [tweaksOpen, setTweaksOpen] = useState(false)
  const [viewFull, setViewFull] = useState(false)
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

  const [accent, setAccent] = useState<AccentState>({ key: 'violet', a: '#7d74f3', b: '#9a92f7' })
  const [density, setDensity] = useState<Density>('comfortable')
  const [columns, setColumns] = useState<Columns>('auto')
  const [insights, setInsights] = useState<InsightsMode>('pinned')

  const openChat = useCallback((prompt?: string) => {
    setChatOpen(true)
    if (prompt) setChatSeed(prompt)
  }, [])
  const closeChat = useCallback(() => setChatOpen(false), [])

  const go = useCallback(
    (next: ScreenKey) => {
      if (valid && next === current) return
      navigate(`/redesign/${next}`)
    },
    [valid, current, navigate],
  )

  // leaving a screen drops any full-bleed detail it had open; screens that
  // deep-link a detail (cases) re-assert viewFull from their own URL state.
  useEffect(() => {
    setViewFull(false)
  }, [current])

  const onPreset = (key: string) => {
    const [a, b] = ACCENTS[key]
    setAccent({ key, a, b })
  }
  const onHex = (input: string): boolean => {
    const a = normHex(input)
    if (!a) return false
    setAccent({ key: null, a, b: lighten(a, 0.22) })
    return true
  }

  const [title, sub] = valid ? TITLES[current] : ['Page not found', 'This page doesn’t exist']
  const Screen = SCREENS[current]

  const wrapperClass = [
    'soc-console',
    density === 'compact' ? 'compact' : '',
    insights === 'inline' ? 'insights-inline' : '',
    chatOpen ? 'chat-active' : '',
  ].filter(Boolean).join(' ')

  const mainClass = ['main', `cols-${columns}`, chatOpen ? 'chat-open' : ''].filter(Boolean).join(' ')

  return (
    <div className={wrapperClass} style={accentVars(accent.a, accent.b)}>
      <div className="shell">
        {/* nav rail */}
        <nav className={`rail${railExpanded ? ' expanded' : ''}`}>
          <button
            className="nav-btn nav-toggle"
            onClick={toggleRail}
            aria-label={railExpanded ? 'Collapse navigation' : 'Expand navigation'}
            aria-expanded={railExpanded}
          >
            <img className="nav-logo mark" src={vigilMark} alt="Vigil" />
            <img className="nav-logo full" src={vigilLogo} alt="Vigil" />
          </button>
          <div className="rail-sep" />
          {NAV.map((n) => {
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
        </nav>

        {/* main */}
        <div className={mainClass}>
          <header className="topbar">
            <div className="title">
              <h1>{title}</h1>
              <p>{sub}</p>
            </div>
            <div className="grow" />
            <button className="btn ghost icon" title="Theme tweaks" onClick={() => setTweaksOpen((v) => !v)}>
              <Icon name="gear" />
            </button>
          </header>
          <main className="view" style={{ overflowY: viewFull ? 'hidden' : 'auto' }}>
            <div className="screen" style={viewFull ? { height: '100%' } : undefined}>
              <ErrorBoundary resetKey={valid ? current : 'notfound'}>
                {valid ? (
                  <Screen openChat={openChat} setViewFull={setViewFull} />
                ) : (
                  <NotFoundScreen path={screen} onHome={() => go('dashboard')} />
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

      <Tweaks
        show={tweaksOpen}
        onClose={() => setTweaksOpen(false)}
        accentKey={accent.key}
        accentHex={accent.a}
        onPreset={onPreset}
        onHex={onHex}
        density={density}
        onDensity={() => setDensity((d) => (d === 'compact' ? 'comfortable' : 'compact'))}
        columns={columns}
        onColumns={setColumns}
        insights={insights}
        onInsights={setInsights}
      />
    </div>
  )
}
