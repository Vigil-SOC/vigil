/* ============================================================
   Settings — left category nav + content panel. Each section is
   its own component wired to the real config APIs. Sections not
   yet ported render a placeholder. Mirrors the legacy Settings.tsx
   tab set (AI Config / Integrations / Users / Auto Investigate /
   Federation / System / General / Developer).
   ============================================================ */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Icon, type IconName } from '../../shared/icons'
import type { ScreenProps, SettingsSectionKey } from '../../shared/types'
import { useToast } from '../../shell/toast'
import AppearanceSection from './AppearanceSection'
import GeneralSection from './GeneralSection'
import SystemSection from './SystemSection'
import FederationSection from './FederationSection'
import UsersSection from './UsersSection'
import AutoInvestigateSection from './AutoInvestigateSection'
import DeveloperSection from './DeveloperSection'
import AiConfigSection from './AiConfigSection'
import IntegrationsSection from './IntegrationsSection'
import SlaPoliciesSection from './SlaPoliciesSection'
import type { SectionProps } from './types'

const IS_DEV_MODE = import.meta.env.VITE_DEV_MODE === 'true'

interface SectionDef {
  key: SettingsSectionKey
  label: string
  icon: IconName
  devOnly?: boolean
  Component?: (props: SectionProps) => JSX.Element
}

const SECTIONS: SectionDef[] = [
  { key: 'appearance', label: 'Appearance', icon: 'palette', Component: AppearanceSection },
  { key: 'ai-config', label: 'AI Config', icon: 'sparkle', Component: AiConfigSection },
  { key: 'integrations', label: 'Integrations', icon: 'link', Component: IntegrationsSection },
  { key: 'users', label: 'Users', icon: 'lock', Component: UsersSection },
  { key: 'sla', label: 'SLA Policies', icon: 'clock', Component: SlaPoliciesSection },
  { key: 'autoinvestigate', label: 'Auto Investigate', icon: 'bolt', Component: AutoInvestigateSection },
  { key: 'federation', label: 'Federation', icon: 'graph', Component: FederationSection },
  { key: 'system', label: 'System', icon: 'wrench', Component: SystemSection },
  { key: 'general', label: 'General', icon: 'gear', Component: GeneralSection },
  { key: 'dev', label: 'Developer', icon: 'fork', devOnly: true, Component: DeveloperSection },
]

export default function SettingsScreen({ setViewFull }: ScreenProps) {
  const sections = useMemo(() => SECTIONS.filter((s) => !s.devOnly || IS_DEV_MODE), [])
  const [searchParams, setSearchParams] = useSearchParams()
  const sectionParam = searchParams.get('section')
  const sectionKeys = useMemo(() => new Set(sections.map((s) => s.key)), [sections])
  const getSection = useCallback(
    (value: string | null): SettingsSectionKey =>
      value && sectionKeys.has(value as SettingsSectionKey) ? (value as SettingsSectionKey) : 'appearance',
    [sectionKeys],
  )
  // open on the first section (Appearance)
  const [active, setActive] = useState<SettingsSectionKey>(() => getSection(sectionParam))
  // section save/test results surface through the shell-wide toast (§10) rather
  // than a settings-local banner, so feedback is consistent across the console
  const { notify } = useToast()

  useEffect(() => {
    setViewFull(true)
    return () => setViewFull(false)
  }, [setViewFull])

  useEffect(() => {
    setActive(getSection(sectionParam))
  }, [getSection, sectionParam])

  const current = sections.find((s) => s.key === active) ?? sections[0]
  const Section = current.Component

  return (
    <div className="settings-wrap">
      <nav className="settings-nav">
        {sections.map((s) => (
          <button
            key={s.key}
            className={`settings-nav-item${s.key === active ? ' active' : ''}`}
            onClick={() => setSearchParams({ section: s.key })}
          >
            <Icon name={s.icon} size={16} />
            <span>{s.label}</span>
          </button>
        ))}
      </nav>

      <div className="settings-content">
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
