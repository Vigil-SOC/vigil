import type { ReactNode } from 'react'
import '../../../../../docs/design/console/tokens/tokens.css'
import '../../styles.css'
import '../../shell/shell.css'
import { useColorScheme } from '../../contexts/ColorSchemeContext'
import { Icon } from '../../shared/icons'
import { VigilLogo } from '../../shared/VigilLogo'

interface Props {
  screenLabel: string
  title: string
  subtitle: string
  children: ReactNode
}

// Brand panel + form panel shared by the sign-in and password-reset screens.
export default function AuthShell({ screenLabel, title, subtitle, children }: Props) {
  const { scheme, setScheme } = useColorScheme()
  const themeLabel = scheme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'

  return (
    <div
      className={`soc-console auth-root ${scheme === 'light' ? 'vg-light' : 'vg-dark'}`}
      data-theme={scheme}
    >
      <div className="auth" data-screen-label={screenLabel}>
        <button
          className="auth-theme"
          type="button"
          onClick={() => setScheme(scheme === 'dark' ? 'light' : 'dark')}
          aria-label={themeLabel}
          title={themeLabel}
        >
          <Icon name={scheme === 'dark' ? 'sun' : 'moon'} size={17} />
        </button>

        <section className="brand">
          <div className="brand-top">
            <VigilLogo className="auth-logo" />
          </div>

          <div className="brand-body">
            <h1>Vigilant, Open, Trustworthy.</h1>
            <p>
              Open source and community built, Vigil stands watch beside
              your analysts on the journey toward greater autonomy.
            </p>
          </div>
        </section>

        <section className="auth-pane">
          <div className="form-wrap">
            <header>
              <h2>{title}</h2>
              <p>{subtitle}</p>
            </header>
            {children}
          </div>
        </section>
      </div>
    </div>
  )
}
