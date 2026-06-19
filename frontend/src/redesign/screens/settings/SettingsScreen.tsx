/* ============================================================
   Settings — left category nav + content panel. Each section is
   its own component wired to the real config APIs. Sections not
   yet ported render a placeholder. Mirrors the legacy Settings.tsx
   tab set (AI Config / Integrations / Users / Auto Investigate /
   Federation / System / General / Developer).
   ============================================================ */
import { useEffect, useRef, useState } from 'react'
import { Icon, type IconName } from '../../shared/icons'
import type { ScreenProps } from '../../shared/types'
import GeneralSection from './GeneralSection'
import SystemSection from './SystemSection'
import FederationSection from './FederationSection'
import UsersSection from './UsersSection'
import AutoInvestigateSection from './AutoInvestigateSection'
import DeveloperSection from './DeveloperSection'
import AiConfigSection from './AiConfigSection'
import IntegrationsSection from './IntegrationsSection'
import type { BannerKind, SectionProps } from './types'

const IS_DEV_MODE = import.meta.env.VITE_DEV_MODE === 'true'

type SectionKey =
  | 'ai-config'
  | 'integrations'
  | 'users'
  | 'autoinvestigate'
  | 'federation'
  | 'system'
  | 'general'
  | 'dev'

interface SectionDef {
  key: SectionKey
  label: string
  icon: IconName
  devOnly?: boolean
  Component?: (props: SectionProps) => JSX.Element
}

const SECTIONS: SectionDef[] = [
  { key: 'ai-config', label: 'AI Config', icon: 'sparkle', Component: AiConfigSection },
  { key: 'integrations', label: 'Integrations', icon: 'link', Component: IntegrationsSection },
  { key: 'users', label: 'Users', icon: 'lock', Component: UsersSection },
  { key: 'autoinvestigate', label: 'Auto Investigate', icon: 'bolt', Component: AutoInvestigateSection },
  { key: 'federation', label: 'Federation', icon: 'graph', Component: FederationSection },
  { key: 'system', label: 'System', icon: 'wrench', Component: SystemSection },
  { key: 'general', label: 'General', icon: 'gear', Component: GeneralSection },
  { key: 'dev', label: 'Developer', icon: 'fork', devOnly: true, Component: DeveloperSection },
]

export default function SettingsScreen({ setViewFull }: ScreenProps) {
  const sections = SECTIONS.filter((s) => !s.devOnly || IS_DEV_MODE)
  // default to the first section that's actually wired up
  const [active, setActive] = useState<SectionKey>('general')
  const [banner, setBanner] = useState<{ kind: BannerKind; text: string } | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    setViewFull(true)
    return () => setViewFull(false)
  }, [setViewFull])

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  const notify = (kind: BannerKind, text: string) => {
    setBanner({ kind, text })
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setBanner(null), kind === 'err' ? 7000 : 4000)
  }

  const current = sections.find((s) => s.key === active) ?? sections[0]
  const Section = current.Component

  return (
    <div className="settings-wrap">
      <nav className="settings-nav">
        {sections.map((s) => (
          <button
            key={s.key}
            className={`settings-nav-item${s.key === active ? ' active' : ''}`}
            onClick={() => { setActive(s.key); setBanner(null) }}
          >
            <Icon name={s.icon} size={16} />
            <span>{s.label}</span>
          </button>
        ))}
      </nav>

      <div className="settings-content">
        {banner && (
          <div className={`settings-banner ${banner.kind}`}>
            <Icon name={banner.kind === 'err' ? 'alert' : banner.kind === 'ok' ? 'check2' : 'info'} size={14} />
            <span>{banner.text}</span>
          </div>
        )}

        {Section ? (
          <Section notify={notify} />
        ) : (
          <div className="settings-placeholder">
            <Icon name={current.icon} size={28} />
            <span className="text-sm">{current.label} settings are coming to the redesign soon.</span>
            <span className="text-xs">Use the classic Settings page for now.</span>
          </div>
        )}
      </div>
    </div>
  )
}
