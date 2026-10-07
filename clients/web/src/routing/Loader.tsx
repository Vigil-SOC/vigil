import '../../../../docs/design/console/tokens/tokens.css'
import '../styles.css'
import '../shell/shell.css'
import { useColorScheme } from '../contexts/ColorSchemeContext'
import { VigilMark } from '../shared/VigilLogo'

export default function Loader({ label = 'Loading console…' }: { label?: string }) {
  const { scheme } = useColorScheme()
  return (
    <div
      className={`soc-console soc-loader ${scheme === 'light' ? 'vg-light' : 'vg-dark'}`}
      data-theme={scheme}
    >
      <div className="soc-loader-inner">
        <VigilMark className="soc-loader-mark" />
        <div className="soc-loader-track" />
        <div className="soc-loader-label">{label}</div>
      </div>
    </div>
  )
}
