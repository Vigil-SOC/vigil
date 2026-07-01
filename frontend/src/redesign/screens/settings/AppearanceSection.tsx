/* ============================================================
   Settings · Appearance — light/dark mode + accent color.
   Replaces the old floating top-bar "tweaks" panel. Mode is the
   app-wide, backend-persisted preference (shared with the legacy
   UI); accent is redesign-only, persisted to localStorage. Both
   are read/written through the redesign theme context.
   ============================================================ */
import { useEffect, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { SettingsCard } from '../../shared/ui'
import { ACCENT_SWATCHES } from '../../shell/accent'
import { BG_SWATCHES } from '../../shell/bg'
import { useSocTheme } from '../../shell/theme'
import type { SectionProps } from './types'

const MODES = [
  { key: 'light', label: 'Light', icon: 'sun' },
  { key: 'dark', label: 'Dark', icon: 'moon' },
] as const

export default function AppearanceSection({ notify }: SectionProps) {
  const { mode, setMode, accent, setPreset, setHex, bg, setBgPreset, setBgHex } = useSocTheme()
  const [hexText, setHexText] = useState(accent.a.replace(/^#/, ''))
  const [bad, setBad] = useState(false)
  const hexRef = useRef<HTMLInputElement>(null)

  const [bgHexText, setBgHexText] = useState(bg.base.replace(/^#/, ''))
  const [bgBad, setBgBad] = useState(false)
  const bgHexRef = useRef<HTMLInputElement>(null)

  // mirror the live accent into the hex field unless the user is typing in it
  useEffect(() => {
    if (document.activeElement !== hexRef.current) {
      setHexText(accent.a.replace(/^#/, ''))
      setBad(false)
    }
  }, [accent.a])

  // mirror the live background base into its hex field unless it's focused
  useEffect(() => {
    if (document.activeElement !== bgHexRef.current) {
      setBgHexText(bg.base.replace(/^#/, ''))
      setBgBad(false)
    }
  }, [bg.base])

  const tryHex = (v: string) => {
    const ok = setHex(v)
    setBad(!ok)
    return ok
  }

  const tryBgHex = (v: string) => {
    const ok = setBgHex(v)
    setBgBad(!ok)
    return ok
  }

  return (
    <>
      <SettingsCard title="Theme" desc="Switch between light and dark mode. Applies everywhere and is remembered for your account.">
        <div className="appr-seg" role="group" aria-label="Theme mode">
          {MODES.map((m) => (
            <button
              key={m.key}
              className={mode === m.key ? 'active' : ''}
              aria-pressed={mode === m.key}
              onClick={() => {
                if (mode === m.key) return
                setMode(m.key)
                notify('ok', `Switched to ${m.label.toLowerCase()} mode.`)
              }}
            >
              <Icon name={m.icon} size={15} />
              {m.label}
            </button>
          ))}
        </div>
      </SettingsCard>

      <SettingsCard title="Accent" desc="Pick a preset or set a custom color. Saved to this browser.">
        <div className="appr-accent">
          <div className="appr-sw-row" role="group" aria-label="Accent presets">
            {ACCENT_SWATCHES.map((s) => (
              <button
                key={s.key}
                className={`appr-sw${accent.key === s.key ? ' active' : ''}`}
                style={{ background: s.color }}
                onClick={() => setPreset(s.key)}
                aria-label={`accent ${s.key}`}
                aria-pressed={accent.key === s.key}
              />
            ))}
          </div>
          <div className="appr-custom">
            <label className="appr-color">
              <span className="appr-color-dot" style={{ background: accent.a }} />
              <input
                type="color"
                value={accent.a}
                onChange={(e) => tryHex(e.target.value)}
                aria-label="Custom accent color"
              />
            </label>
            <div className={`appr-hex${bad ? ' bad' : ''}`}>
              <span>#</span>
              <input
                ref={hexRef}
                type="text"
                maxLength={6}
                spellCheck={false}
                placeholder="7d74f3"
                value={hexText}
                aria-label="Custom accent hex"
                onChange={(e) => {
                  setHexText(e.target.value)
                  setBad(false)
                  tryHex(e.target.value)
                }}
                onBlur={() => tryHex(hexText)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    tryHex(hexText)
                    hexRef.current?.blur()
                  }
                }}
              />
            </div>
          </div>
        </div>
      </SettingsCard>

      <SettingsCard
        title="Background"
        desc="Pick a preset or set a custom base color — the whole surface and text ramp is derived from it. Light vs. dark mode follows the base you choose. Saved to this browser."
      >
        <div className="appr-accent">
          <div className="appr-sw-row" role="group" aria-label="Background presets">
            {BG_SWATCHES.map((s) => (
              <button
                key={s.key}
                className={`appr-sw appr-bg-sw${bg.key === s.key ? ' active' : ''}`}
                style={{ background: s.color }}
                onClick={() => setBgPreset(s.key)}
                aria-label={`background ${s.key}`}
                aria-pressed={bg.key === s.key}
              />
            ))}
          </div>
          <div className="appr-custom">
            <label className="appr-color">
              <span className="appr-color-dot" style={{ background: bg.base }} />
              <input
                type="color"
                value={bg.base}
                onChange={(e) => tryBgHex(e.target.value)}
                aria-label="Custom background color"
              />
            </label>
            <div className={`appr-hex${bgBad ? ' bad' : ''}`}>
              <span>#</span>
              <input
                ref={bgHexRef}
                type="text"
                maxLength={6}
                spellCheck={false}
                placeholder="0c0f14"
                value={bgHexText}
                aria-label="Custom background hex"
                onChange={(e) => {
                  setBgHexText(e.target.value)
                  setBgBad(false)
                  tryBgHex(e.target.value)
                }}
                onBlur={() => tryBgHex(bgHexText)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    tryBgHex(bgHexText)
                    bgHexRef.current?.blur()
                  }
                }}
              />
            </div>
          </div>
        </div>
      </SettingsCard>
    </>
  )
}
