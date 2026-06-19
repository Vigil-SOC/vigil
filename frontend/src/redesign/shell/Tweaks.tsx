/* ============================================================
   Lightweight theme tweaks panel — accent presets, custom hex,
   density, adaptive columns, insights-rail mode.
   Ported from the design's #tweaks markup + main.js handlers.
   ============================================================ */
import { useEffect, useRef, useState } from 'react'
import { ACCENT_SWATCHES } from './accent'

export type Density = 'comfortable' | 'compact'
export type Columns = 'auto' | 'all' | 'essential'
export type InsightsMode = 'pinned' | 'inline'

interface TweaksProps {
  show: boolean
  onClose: () => void
  accentKey: string | null
  accentHex: string
  onPreset: (key: string) => void
  /** apply a free-typed/picked hex; returns true if it was valid */
  onHex: (hex: string) => boolean
  density: Density
  onDensity: () => void
  columns: Columns
  onColumns: (c: Columns) => void
  insights: InsightsMode
  onInsights: (i: InsightsMode) => void
}

export default function Tweaks({
  show,
  onClose,
  accentKey,
  accentHex,
  onPreset,
  onHex,
  density,
  onDensity,
  columns,
  onColumns,
  insights,
  onInsights,
}: TweaksProps) {
  const [hexText, setHexText] = useState(accentHex.replace(/^#/, ''))
  const [bad, setBad] = useState(false)
  const hexRef = useRef<HTMLInputElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  // close on Esc and return focus to the element that opened the panel
  useEffect(() => {
    if (!show) return
    const opener = document.activeElement as HTMLElement | null
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      opener?.focus?.()
    }
  }, [show, onClose])

  // mirror the live accent into the hex field unless the user is typing in it
  useEffect(() => {
    if (document.activeElement !== hexRef.current) {
      setHexText(accentHex.replace(/^#/, ''))
      setBad(false)
    }
  }, [accentHex])

  const tryHex = (v: string) => {
    const ok = onHex(v)
    setBad(!ok)
    return ok
  }

  return (
    <div
      ref={panelRef}
      className={`tweaks${show ? ' show' : ''}`}
      role="dialog"
      aria-label="Theme tweaks"
      aria-hidden={!show}
    >
      <div className="tw-grp">
        <span className="tw-label">Accent</span>
        {ACCENT_SWATCHES.map((s) => (
          <button
            key={s.key}
            className={`sw${accentKey === s.key ? ' active' : ''}`}
            style={{ background: s.color }}
            onClick={() => onPreset(s.key)}
            aria-label={`accent ${s.key}`}
          />
        ))}
      </div>
      <div className="sep" />
      <div className="tw-grp">
        <span className="tw-label">Custom</span>
        <label className="tw-color">
          <span className="tw-color-dot" style={{ background: accentHex }} />
          <input type="color" value={accentHex} onChange={(e) => tryHex(e.target.value)} />
        </label>
        <div className="tw-hex">
          <span>#</span>
          <input
            ref={hexRef}
            type="text"
            maxLength={6}
            spellCheck={false}
            placeholder="7d74f3"
            className={bad ? 'bad' : ''}
            value={hexText}
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
      <div className="sep" />
      <div className="tw-grp">
        <span className="tw-label">Density</span>
        <button className={`tw-toggle${density === 'compact' ? ' on' : ''}`} onClick={onDensity}>
          {density === 'compact' ? 'Compact' : 'Comfortable'}
        </button>
      </div>
      <div className="sep" />
      <div className="tw-grp">
        <span className="tw-label">Columns</span>
        <div className="tw-seg">
          {(['auto', 'all', 'essential'] as Columns[]).map((c) => (
            <button key={c} className={columns === c ? 'active' : ''} onClick={() => onColumns(c)}>
              {c[0].toUpperCase() + c.slice(1)}
            </button>
          ))}
        </div>
      </div>
      <div className="sep" />
      <div className="tw-grp">
        <span className="tw-label">Insights rail</span>
        <div className="tw-seg">
          {(['pinned', 'inline'] as InsightsMode[]).map((i) => (
            <button key={i} className={insights === i ? 'active' : ''} onClick={() => onInsights(i)}>
              {i[0].toUpperCase() + i.slice(1)}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
