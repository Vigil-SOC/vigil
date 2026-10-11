import { Icon, type IconName } from '../../shared/icons'
import { SettingsCard } from '../../shared/ui'
import type { CheckPhase } from './CheckMark'
import CheckTable from './CheckTable'
import type { SystemChecks } from './useSystemChecks'

export type { CheckPhase }

const NEEDS: { icon: IconName; title: string; sub: string }[] = [
  {
    icon: 'link',
    title: 'Access to one alert source',
    sub: 'A SIEM, EDR or identity tool, or a file export to upload',
  },
  {
    icon: 'bot',
    title: 'An AI provider key, or a local model',
    sub: 'Anthropic, OpenAI or your cloud; or an Ollama server for fully local use',
  },
  { icon: 'clock', title: 'About 15 minutes', sub: 'Nothing runs on its own until you finish' },
]

type Props = Pick<SystemChecks, 'checks' | 'warnings' | 'rerun'>

export default function SystemChecksStep({ checks, warnings, rerun }: Props) {
  return (
    <>
      <SettingsCard
        title="System checks"
        desc="Vigil checked this server before you start."
        actions={
          <button className="btn ghost" onClick={rerun}>
            <Icon name="refresh" size={14} /> Check again
          </button>
        }
      >
        <CheckTable
          rows={checks}
          label="System checks"
          summary={
            warnings
              ? {
                  tone: 'fair',
                  text: `${warnings} ${warnings === 1 ? 'warning' : 'warnings'}. You can continue; fix them before you rely on alerts.`,
                }
              : undefined
          }
        />
      </SettingsCard>
      <SettingsCard title="What you'll need">
        <ul className="su-needs">
          {NEEDS.map((need) => (
            <li key={need.title}>
              <span className="su-needs-icon">
                <Icon name={need.icon} size={16} />
              </span>
              <span>
                <span className="su-needs-t">{need.title}</span>
                <span className="su-needs-s">{need.sub}</span>
              </span>
            </li>
          ))}
        </ul>
      </SettingsCard>
    </>
  )
}
