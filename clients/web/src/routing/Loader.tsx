/* ============================================================
   Route-transition loading screen — the Suspense fallback in
   App.tsx while the lazy console / login / setup chunks load, and
   what the route guards show while auth and first-run setup
   resolve. Themed via ColorSchemeContext and the persisted accent,
   so it matches whatever renders once it mounts.
   ============================================================ */
import '../styles.css'
import { useColorScheme } from '../contexts/ColorSchemeContext'
import { VigilMark } from '../shared/VigilLogo'
import { accentVars } from '../shell/accent'

const DEFAULT_ACCENT: [string, string] = ['#7d74f3', '#9a92f7']

/** read the accent the user last picked (Appearance settings) so the loader's
 *  brand glyph + progress bar match the console; falls back to the default. */
function readAccent(): [string, string] {
  try {
    const raw = localStorage.getItem('soc.accent')
    if (raw) {
      const p = JSON.parse(raw) as { a?: string; b?: string }
      if (typeof p?.a === 'string' && typeof p?.b === 'string') return [p.a, p.b]
    }
  } catch {
    /* malformed / unavailable localStorage — fall back to the default */
  }
  return DEFAULT_ACCENT
}

export default function Loader({ label = 'Loading console…' }: { label?: string }) {
  const { scheme } = useColorScheme()
  const [a, b] = readAccent()
  return (
    <div className="soc-console soc-loader" data-theme={scheme} style={accentVars(a, b)}>
      <div className="soc-loader-inner">
        <VigilMark className="soc-loader-mark" />
        <div className="soc-loader-track" />
        <div className="soc-loader-label">{label}</div>
      </div>
    </div>
  )
}
