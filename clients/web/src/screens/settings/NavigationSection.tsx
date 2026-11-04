import { useState } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { Icon } from '../../shared/icons'
import { SettingsCard } from '../../shared/ui'
import { splitNav, useConsoleNav } from '../../shell/consoleNav'
import { NAV_SHOWN_DEFAULT, useNavPrefs, writeNavPrefs, type NavLayout } from '../../shell/navPrefs'
import './navigation.css'

const LAYOUTS: [NavLayout, string][] = [
  ['vertical', 'Vertical'],
  ['top', 'Top'],
]

export default function NavigationSection() {
  const userId = useAuth().user?.user_id
  const { layout, shown, iconsOnly } = useNavPrefs(userId)
  const { visibleNav } = useConsoleNav()
  const { bar, rest } = splitNav(visibleNav, shown)
  const barKeys = bar.map((item) => item[2])
  const [dragKey, setDragKey] = useState<string | null>(null)

  // Keys this list can't see (an extension still loading, a page not permitted) keep their stored place at the end.
  const saveBar = (keys: string[]) => {
    const seen = new Set(visibleNav.map((item) => item[2]))
    writeNavPrefs(userId, { shown: [...keys, ...shown.filter((key) => !seen.has(key))] })
  }
  const move = (key: string, to: number) => {
    const keys = barKeys.filter((k) => k !== key)
    // Home stays first
    keys.splice(Math.max(to, barKeys[0] === 'home' ? 1 : 0), 0, key)
    saveBar(keys)
  }

  return (
    <>
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
      <SettingsCard
        title="Pages on the bar"
        desc="Choose which pages sit on the navigation bar and in what order; the rest are under More. Changes apply as you make them. On a narrow window the top bar folds what doesn’t fit into More, starting from the end."
        actions={
          <button
            type="button"
            className="btn ghost"
            onClick={() => writeNavPrefs(userId, { shown: NAV_SHOWN_DEFAULT, iconsOnly: false })}
          >
            Reset to default
          </button>
        }
      >
        <div className="nav-pref-labels">
          <div className="lim-seg" role="group" aria-label="Tab labels">
            <button type="button" aria-pressed={!iconsOnly} onClick={() => writeNavPrefs(userId, { iconsOnly: false })}>
              Icons &amp; text
            </button>
            <button type="button" aria-pressed={iconsOnly} onClick={() => writeNavPrefs(userId, { iconsOnly: true })}>
              Icons only
            </button>
          </div>
        </div>
        <ul className="nav-pref-list" aria-label="Pages">
          {[...bar, ...rest].map(([icon, label, key]) => {
            const onBar = barKeys.includes(key)
            const locked = key === 'home'
            const at = barKeys.indexOf(key)
            const movable = onBar && !locked
            const canUp = movable && at > (barKeys[0] === 'home' ? 1 : 0)
            const canDown = movable && at < barKeys.length - 1
            return (
              <li
                key={key}
                className={`nav-pref-row${onBar ? '' : ' off'}${dragKey === key ? ' dragging' : ''}`}
                draggable={movable}
                onDragStart={(e) => {
                  e.dataTransfer.effectAllowed = 'move'
                  e.dataTransfer.setData('text/plain', key)
                  setDragKey(key)
                }}
                onDragEnd={() => setDragKey(null)}
                onDragOver={(e) => {
                  if (dragKey && movable) e.preventDefault()
                }}
                onDrop={(e) => {
                  e.preventDefault()
                  if (dragKey && dragKey !== key && movable) move(dragKey, at)
                  setDragKey(null)
                }}
              >
                <span className={`nav-pref-grip${movable ? '' : ' nav-pref-hidden'}`} aria-hidden="true" title="Drag to reorder">
                  <Icon name="menu" size={14} />
                </span>
                <Icon name={icon} size={15} />
                <span className="nav-pref-label">{label}</span>
                <span className="nav-pref-ctrl">
                  {locked && <span className="nav-pref-pin">Always first</span>}
                  <label className="nav-pref-show">
                    <input
                      type="checkbox"
                      checked={onBar}
                      disabled={locked}
                      onChange={(e) => saveBar(e.target.checked ? [...barKeys, key] : barKeys.filter((k) => k !== key))}
                    />
                    Show on the bar
                  </label>
                  <button
                    type="button"
                    className={`btn ghost icon${movable ? '' : ' nav-pref-hidden'}`}
                    aria-label={`Move ${label} up`}
                    title="Move up"
                    // aria-disabled, so focus stays on the button when the row reaches an end
                    aria-disabled={!canUp}
                    onClick={() => {
                      if (canUp) move(key, at - 1)
                    }}
                  >
                    <Icon name="arrowUp" size={13} />
                  </button>
                  <button
                    type="button"
                    className={`btn ghost icon${movable ? '' : ' nav-pref-hidden'}`}
                    aria-label={`Move ${label} down`}
                    title="Move down"
                    // aria-disabled, so focus stays on the button when the row reaches an end
                    aria-disabled={!canDown}
                    onClick={() => {
                      if (canDown) move(key, at + 1)
                    }}
                  >
                    <Icon name="arrowDn" size={13} />
                  </button>
                </span>
              </li>
            )
          })}
        </ul>
      </SettingsCard>
    </>
  )
}
