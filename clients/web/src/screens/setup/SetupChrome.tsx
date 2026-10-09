import { useEffect, useState } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { consoleApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import { VigilLogo } from '../../shared/VigilLogo'
import { feedbackHref, userInitials } from '../../shell/UserMenu'

export interface RailStep {
  title: string
  sub: string
}

export function TopBar() {
  const { user } = useAuth()
  const [version, setVersion] = useState('')

  useEffect(() => {
    let live = true
    consoleApi
      .getHealth()
      .then((res) => {
        const value = (res.data as { version?: string } | undefined)?.version
        if (live && value) setVersion(value)
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])

  return (
    <header className="su-top">
      <div className="su-brand">
        <VigilLogo className="vg-logo" />
        <span className="su-brand-sep" />
        <span className="su-brand-name">Setup</span>
      </div>
      <div className="su-top-end">
        <a
          className="su-feedback"
          href={feedbackHref('setup', version)}
          target="_blank"
          rel="noreferrer"
        >
          <Icon name="feedback" size={15} />
          Feedback
        </a>
        {user && <span className="vg-avatar" aria-label="Signed in as you">{userInitials(user)}</span>}
      </div>
    </header>
  )
}

interface RailProps {
  steps: readonly RailStep[]
  /** zero-based; -1 when no step is active (the done page) */
  active: number
  passed: (n: number) => boolean
  onPick: (index: number) => void
  onDemo: () => void
  demoBusy: boolean
}

export function Rail({ steps, active, passed, onPick, onDemo, demoBusy }: RailProps) {
  return (
    <aside className="su-rail" aria-label="Setup steps">
      <span className="su-rail-head">Set up Vigil</span>
      {steps.map((s, i) => (
        <button
          key={s.title}
          type="button"
          className={`su-rail-item${i === active ? ' on' : ''}${passed(i + 1) ? ' passed' : ''}`}
          aria-current={i === active ? 'step' : undefined}
          onClick={() => onPick(i)}
        >
          <span className="su-rail-n">
            {passed(i + 1) ? <Icon name="check" size={13} /> : i + 1}
          </span>
          <span>
            <span className="su-rail-t">{s.title}</span>
            <span className="su-rail-s">{s.sub}</span>
          </span>
        </button>
      ))}
      <span className="su-rail-gap" />
      <div className="su-guide">
        <span className="su-guide-t">
          <Icon name="info" size={15} />
          Guidance continues after setup
        </span>
        <p>
          Setup in the profile menu brings you back here, Home keeps a checklist, and empty screens
          tell you what to connect.
        </p>
      </div>
      <button type="button" className="su-demo" disabled={demoBusy} onClick={onDemo}>
        Skip setup and look around with demo data
      </button>
    </aside>
  )
}
