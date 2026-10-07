import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Icon, type IconName } from '../../shared/icons'
import { NotMeasured } from '../../shared/NotMeasured'
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
import ServicesSection from './ServicesSection'
import IntegrationsSection from './IntegrationsSection'
import SlaPoliciesSection from './SlaPoliciesSection'
import DataIngestionPanel from './DataIngestion'
import DetectionRulesPanel from './DetectionRulesPanel'
import type { SectionProps } from './types'

const IS_DEV_MODE = import.meta.env.VITE_DEV_MODE === 'true'

type NavKey =
  | 'ai-config'
  | 'integrations'
  | 'federation'
  | 'sla'
  | 'autoinvestigate'
  | 'data'
  | 'system'

type SystemTabKey = 'services' | 'system' | 'general' | 'dev' | 'users'

interface NavDef {
  key: NavKey
  label: string
  desc: string
  icon: IconName
  Component: (props: SectionProps) => JSX.Element
}

interface SystemTabDef {
  key: SystemTabKey
  label: string
  devOnly?: boolean
  Component: (props: SectionProps) => JSX.Element
}

const SYSTEM_TAB_DEFS: SystemTabDef[] = [
  { key: 'services', label: 'Services', Component: ServicesSection },
  { key: 'system', label: 'System', Component: SystemSection },
  { key: 'general', label: 'General', Component: GeneralSection },
  { key: 'dev', label: 'Developer', devOnly: true, Component: DeveloperSection },
  { key: 'users', label: 'Users', Component: UsersSection },
]
const SYSTEM_TABS = SYSTEM_TAB_DEFS.filter((tab) => !tab.devOnly || IS_DEV_MODE)

const SYSTEM_KEYS = new Set<string>(SYSTEM_TABS.map((tab) => tab.key))

type DataTab = 'ingestion' | 'detection'

function dataTabFromQuery(value: string | null): DataTab {
  if (value === 'detection') return 'detection'
  return 'ingestion'
}

function DataUploadsSection({ notify }: SectionProps) {
  const [searchParams] = useSearchParams()
  const requested = dataTabFromQuery(searchParams.get('tab'))
  const [tab, setTab] = useState<DataTab>(requested)

  useEffect(() => {
    setTab(requested)
  }, [requested])

  return (
    <>
      <NotMeasured label="Retention" tip="Vigil does not record how long uploaded data is kept." />
      <div className="tabs" style={{ gap: 4 }}>
        <button className={`tab${tab === 'ingestion' ? ' active' : ''}`} onClick={() => setTab('ingestion')}>
          Manual Upload
        </button>
        <button className={`tab${tab === 'detection' ? ' active' : ''}`} onClick={() => setTab('detection')}>
          Detection Rules
        </button>
      </div>
      {tab === 'ingestion' && <DataIngestionPanel notify={notify} />}
      {tab === 'detection' && <DetectionRulesPanel notify={notify} />}
    </>
  )
}

function SystemTabs({ notify }: SectionProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const sectionParam = searchParams.get('section')
  const tab: SystemTabKey =
    sectionParam && SYSTEM_KEYS.has(sectionParam) ? (sectionParam as SystemTabKey) : 'system'
  const current = SYSTEM_TABS.find((item) => item.key === tab) ?? SYSTEM_TABS.find((item) => item.key === 'system')!
  const Panel = current.Component

  return (
    <>
      <div className="tabs" style={{ gap: 4 }}>
        {SYSTEM_TABS.map((item) => (
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
  { key: 'ai-config', label: 'AI models', desc: 'Which models Vigil uses, for which agent, and what happens when one is unavailable. Keys are stored encrypted and never shown again.', icon: 'sparkle', Component: AiConfigSection },
  { key: 'integrations', label: 'Integrations', desc: 'The tools Vigil reads from and acts through.', icon: 'link', Component: IntegrationsSection },
  { key: 'federation', label: 'Alert collection', desc: 'Pull alerts from your SIEM and EDR tools on a schedule, so agents can start on them without anyone forwarding them.', icon: 'graph', Component: FederationSection },
  { key: 'sla', label: 'SLA policies', desc: 'How fast a case must get a first response and be resolved, by severity.', icon: 'clock', Component: SlaPoliciesSection },
  { key: 'autoinvestigate', label: 'Limits & autonomy', desc: 'How much Vigil may do without you: whether agents start on their own, how many run at once, and what they may spend.', icon: 'bolt', Component: AutoInvestigateSection },
  { key: 'data', label: 'Data & uploads', desc: 'Bring data in without a live connector, and manage the detection rules applied to it.', icon: 'upload', Component: DataUploadsSection },
  { key: 'system', label: 'System', desc: 'Service health, system information, general options and users.', icon: 'wrench', Component: SystemTabs },
]

const NAV_KEYS = new Set<string>(NAV.map((item) => item.key))

function resolveNav(sectionParam: string | null): NavKey {
  // Old bookmarks for the panels that now live under System stay on that item.
  if (sectionParam && SYSTEM_KEYS.has(sectionParam)) return 'system'
  if (sectionParam && NAV_KEYS.has(sectionParam)) return sectionParam as NavKey
  return NAV[0].key
}

export default function SettingsScreen({ setViewFull }: ConsoleScreenProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const sectionParam = searchParams.get('section')
  const active = resolveNav(sectionParam)
  const { notify } = useToast()

  useEffect(() => {
    setViewFull(true)
    return () => setViewFull(false)
  }, [setViewFull])

  const current = NAV.find((item) => item.key === active) ?? NAV[0]
  const Section = current.Component

  return (
    <div className="settings-wrap">
      <nav className="settings-nav" aria-label="Settings sections">
        <h1 className="settings-nav-title">Settings</h1>
        {NAV.map((item) => (
          <button
            key={item.key}
            className={`settings-nav-item${item.key === active ? ' active' : ''}`}
            aria-current={item.key === active ? 'page' : undefined}
            onClick={() => setSearchParams({ section: item.key }, { replace: true })}
          >
            <Icon name={item.icon} size={16} />
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="settings-content">
        <PageHead title={current.label} description={current.desc} />
        <Section notify={notify} />
      </div>
    </div>
  )
}
