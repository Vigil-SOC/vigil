import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Icon, type IconName } from '../../shared/icons'
import type { ConsoleScreenProps } from '../../shared/types'
import { useToast } from '../../shell/toast'
import AppearanceSection from './AppearanceSection'
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
  | 'appearance'
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
      <p className="text-sm text-tx-3">Retention: Not measured yet</p>
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
  { key: 'appearance', label: 'Appearance', icon: 'palette', Component: AppearanceSection },
  { key: 'ai-config', label: 'AI models', icon: 'sparkle', Component: AiConfigSection },
  { key: 'integrations', label: 'Integrations', icon: 'link', Component: IntegrationsSection },
  { key: 'federation', label: 'Alert collection', icon: 'graph', Component: FederationSection },
  { key: 'sla', label: 'SLA policies', icon: 'clock', Component: SlaPoliciesSection },
  { key: 'autoinvestigate', label: 'Limits & autonomy', icon: 'bolt', Component: AutoInvestigateSection },
  { key: 'data', label: 'Data & uploads', icon: 'upload', Component: DataUploadsSection },
  { key: 'system', label: 'System', icon: 'wrench', Component: SystemTabs },
]

const NAV_KEYS = new Set<string>(NAV.map((item) => item.key))

function resolveNav(sectionParam: string | null): NavKey {
  // Old bookmarks for the panels that now live under System stay on that item.
  if (sectionParam && SYSTEM_KEYS.has(sectionParam)) return 'system'
  if (sectionParam && NAV_KEYS.has(sectionParam)) return sectionParam as NavKey
  return 'appearance'
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
      <nav className="settings-nav">
        {NAV.map((item) => (
          <button
            key={item.key}
            className={`settings-nav-item${item.key === active ? ' active' : ''}`}
            onClick={() => setSearchParams({ section: item.key }, { replace: true })}
          >
            <Icon name={item.icon} size={16} />
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="settings-content">
        <Section notify={notify} />
      </div>
    </div>
  )
}
