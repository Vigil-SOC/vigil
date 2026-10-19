import type { ReactNode } from 'react'
import '../../../../../docs/design/console/tokens/tokens.css'
import '../../styles.css'
import '../../shell/shell.css'
import { Icon } from '../../shared/icons'
import { VigilLogo } from '../../shared/VigilLogo'
import { useColorScheme } from '../../contexts/ColorSchemeContext'

interface Props {
  screenLabel: string
  title: string
  subtitle: string
  // `info` renders the accent notice style instead of the error style
  message?: { text: string; info?: boolean }
  children: ReactNode
}

// The sign-in shell (brand panel + form pane) shared by sign-in, first-account
// creation, and password reset.
export default function AuthLayout({ screenLabel, title, subtitle, message, children }: Props) {
  const { scheme, setScheme } = useColorScheme()

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
          aria-label={scheme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          title={scheme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          <Icon name={scheme === 'dark' ? 'sun' : 'moon'} size={17} />
        </button>

        {/* ---------- brand panel ---------- */}
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

        {/* ---------- form panel ---------- */}
        <section className="auth-pane">
          <div className="form-wrap">
            <header>
              <h2>{title}</h2>
              <p>{subtitle}</p>
            </header>

            {message?.text && (
              <div className={`auth-error${message.info ? ' info' : ''}`} role="alert">
                <Icon name={message.info ? 'info' : 'alert'} size={15} />
                <span>{message.text}</span>
              </div>
            )}

            {children}
          </div>
        </section>
      </div>
    </div>
  )
}
