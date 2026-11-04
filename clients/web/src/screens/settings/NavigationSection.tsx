import { useAuth } from '../../contexts/AuthContext'
import { SettingsCard } from '../../shared/ui'
import { useNavPrefs, writeNavPrefs, type NavLayout } from '../../shell/navPrefs'

const LAYOUTS: [NavLayout, string][] = [
  ['vertical', 'Vertical'],
  ['top', 'Top'],
]

export default function NavigationSection() {
  const userId = useAuth().user?.user_id
  const { layout } = useNavPrefs(userId)
  return (
    <SettingsCard
      title="Layout"
      desc="Where the console’s navigation sits: down the left side, or across the top. Windows 600px wide or narrower always use the top bar."
    >
      <div className="lim-seg" role="group" aria-label="Navigation layout">
        {LAYOUTS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            aria-pressed={layout === key}
            onClick={() => writeNavPrefs(userId, { layout: key })}
          >
            {label}
          </button>
        ))}
      </div>
    </SettingsCard>
  )
}
