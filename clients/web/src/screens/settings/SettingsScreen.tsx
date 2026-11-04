import { useEffect, type JSX } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Icon, type IconName } from '../../shared/icons'
import { PageHead } from '../../shared/PageHead'
import type { ConsoleScreenProps } from '../../shared/types'
import { useToast } from '../../shell/toast'
import GeneralSection from './GeneralSection'
import SystemSection from './SystemSection'
import FederationSection from './FederationSection'
import UsersSection from './UsersSection'
import AutoInvestigateSection from './AutoInvestigateSection'
import DeveloperSection from './DeveloperSection'
import AiConfigSection from './AiConfigSection'
import { AI_CONFIG_DESC } from '../../config/aiComponents'
import ServicesSection from './ServicesSection'
import IntegrationsSection from './IntegrationsSection'
import { INTEGRATIONS_DESC } from './integrationsData'
import { IntegrationsStateProvider, useIntegrationsState } from './IntegrationsState'
import SlaPoliciesSection, { SLA_DESC } from './SlaPoliciesSection'
import DataUploadsSection from './DataUploadsSection'
import AuditSection, { AUDIT_DESC } from './AuditSection'
import NavigationSection from './NavigationSection'
import { useAuth } from '../../contexts/AuthContext'
import type { SectionProps } from './types'

const IS_DEV_MODE = import.meta.env.VITE_DEV_MODE === 'true'

type NavKey =
  | 'ai-config'
  | 'integrations'
  | 'federation'
  | 'sla'
  | 'autoinvestigate'
  | 'data'
  | 'audit'
  | 'system'

type SystemTabKey = 'services' | 'system' | 'general' | 'dev' | 'users' | 'appearance'

interface NavDef {
  key: NavKey
  label: string
  desc: string
  icon: IconName
  Component: (props: SectionProps) => JSX.Element
  /** the section renders its own PageHead (it has head actions) */
  ownsHead?: boolean
  /** shown only to holders of this permission; absent, shown to everyone */
  perm?: string
}

interface SystemTabDef {
  key: SystemTabKey
  label: string
  devOnly?: boolean
  /** shown only to holders of this permission; absent, shown to everyone */
  perm?: string
  Component: (props: SectionProps) => JSX.Element
}

const SYSTEM_TAB_DEFS: SystemTabDef[] = [
  { key: 'services', label: 'Services', perm: 'platform.read', Component: ServicesSection },
  { key: 'system', label: 'System', perm: 'platform.read', Component: SystemSection },
  { key: 'general', label: 'General', perm: 'platform.read', Component: GeneralSection },
  { key: 'dev', label: 'Developer', devOnly: true, perm: 'platform.read', Component: DeveloperSection },
  { key: 'users', label: 'Users', perm: 'platform.read', Component: UsersSection },
  // a person's own view of the console, not the platform's: ungated
  { key: 'appearance', label: 'Appearance', Component: NavigationSection },
]
const SYSTEM_TABS = SYSTEM_TAB_DEFS.filter((tab) => !tab.devOnly || IS_DEV_MODE)

// 'navigation' is the old Settings item the Appearance tab replaced
const SYSTEM_KEYS = new Set<string>([...SYSTEM_TABS.map((tab) => tab.key), 'navigation'])

function SystemTabs({ notify }: SectionProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const { hasPermission } = useAuth()
  const tabs = SYSTEM_TABS.filter((item) => !item.perm || hasPermission(item.perm))
  const param = searchParams.get('section')
  const fallback = tabs.find((item) => item.key === 'system') ?? tabs.find((item) => item.key === 'appearance')!
  const current = tabs.find((item) => item.key === (param === 'navigation' ? 'appearance' : param)) ?? fallback
  const tab = current.key
  const Panel = current.Component

  return (
    <>
      {/* wraps on a narrow window so every tab stays reachable */}
      <div className="tabs" style={{ gap: '18px 4px', flexWrap: 'wrap' }}>
        {tabs.map((item) => (
          <button
            key={item.key}
            className={`tab${item.key === tab ? ' active' : ''}`}
            onClick={() => setSearchParams({ section: item.key }, { replace: true })}
          >
            {item.label}
          </button>
        ))}
      </div>
      <Panel notify={notify} />
    </>
  )
}

const NAV: NavDef[] = [
  { key: 'ai-config', label: 'AI models', desc: AI_CONFIG_DESC, icon: 'sparkle', Component: AiConfigSection, ownsHead: true, perm: 'platform.read' },
  { key: 'integrations', label: 'Integrations', desc: INTEGRATIONS_DESC, icon: 'link', Component: IntegrationsSection, ownsHead: true, perm: 'integrations.read' },
  { key: 'federation', label: 'Alert collection', desc: 'Pull alerts from your SIEM and EDR tools on a schedule, so agents can start on them without anyone forwarding them.', icon: 'graph', Component: FederationSection, perm: 'platform.read' },
  { key: 'sla', label: 'SLA policies', desc: SLA_DESC, icon: 'clock', Component: SlaPoliciesSection, ownsHead: true, perm: 'platform.read' },
  { key: 'autoinvestigate', label: 'Limits & autonomy', desc: 'How much Vigil may do without you: whether agents start on their own, how many run at once, what they may spend, and which tools may act alone.', icon: 'bolt', Component: AutoInvestigateSection, perm: 'ai_autonomy.read' },
  { key: 'data', label: 'Data & uploads', desc: 'Bring data in without a live connector, and manage the detection rules applied to it.', icon: 'upload', Component: DataUploadsSection, perm: 'platform.read' },
  { key: 'audit', label: 'Audit', desc: AUDIT_DESC, icon: 'shield', Component: AuditSection, ownsHead: true, perm: 'audit.read' },
  // ungated for the Appearance tab; its admin tabs gate themselves
  { key: 'system', label: 'System', desc: 'Service health, system information, general options, users, and how the console looks for you.', icon: 'wrench', Component: SystemTabs },
]

function resolveNav(sectionParam: string | null, nav: NavDef[]): NavKey {
  // Old bookmarks for the panels that now live under System stay on that item.
  if (sectionParam && SYSTEM_KEYS.has(sectionParam)) return 'system'
  if (sectionParam && nav.some((item) => item.key === sectionParam)) return sectionParam as NavKey
  return nav[0].key
}

export default function SettingsScreen(props: ConsoleScreenProps) {
  return (
    <IntegrationsStateProvider>
      <SettingsLayout {...props} />
    </IntegrationsStateProvider>
  )
}

function SettingsLayout({ setViewFull }: ConsoleScreenProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const sectionParam = searchParams.get('section')
  const { hasPermission } = useAuth()
  const nav = NAV.filter((item) => !item.perm || hasPermission(item.perm))
  const active = resolveNav(sectionParam, nav)
  const { notify } = useToast()
  const { attention } = useIntegrationsState()

  useEffect(() => {
    setViewFull(true)
    return () => setViewFull(false)
  }, [setViewFull])

  // System is ungated, so there is always a section
  const current = nav.find((item) => item.key === active) ?? nav[0]
  const Section = current.Component

  return (
    <div className="settings-wrap">
      <nav className="settings-nav" aria-label="Settings sections">
        <h1 className="settings-nav-title">Settings</h1>
        {nav.map((item) => (
          <button
            key={item.key}
            className={`settings-nav-item${item.key === active ? ' active' : ''}`}
            aria-current={item.key === active ? 'page' : undefined}
            onClick={() => setSearchParams({ section: item.key }, { replace: true })}
          >
            <Icon name={item.icon} size={16} />
            <span>{item.label}</span>
            {item.key === 'integrations' && attention.length > 0 && (
              <span className="settings-nav-count" aria-label={`${attention.length} need attention`}>{attention.length}</span>
            )}
          </button>
        ))}
      </nav>

      <div className="settings-content">
        {!current.ownsHead && <PageHead title={current.label} description={current.desc} />}
        <Section notify={notify} />
      </div>
    </div>
  )
}
