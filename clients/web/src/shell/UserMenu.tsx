import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate, useParams } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { consoleApi } from '../services/api'
import { Icon } from '../shared/icons'
import { useColorScheme } from '../contexts/ColorSchemeContext'
import TwoStepDialog from './TwoStepDialog'
import ChangePasswordDialog from './ChangePasswordDialog'

const FEEDBACK_BASE =
  import.meta.env.VITE_FEEDBACK_URL ||
  'https://github.com/Vigil-SOC/vigil/issues/new?labels=console-feedback'

// With auth bypassed there is no real account behind the mocked user, so
// there is no password to change. The backend can say so at runtime too.
const BUILT_WITH_BYPASS = import.meta.env.VITE_DEV_MODE === 'true'

export function feedbackHref(screen: string, version: string): string {
  const url = new URL(FEEDBACK_BASE)
  url.searchParams.set('title', `Console feedback: ${screen}`)
  url.searchParams.set('body', `Screen: ${screen}\nVersion: ${version || 'unknown'}\n\n`)
  return url.toString()
}

/** two-letter avatar text; /auth/me can send null/omitted full_name even though the TS type is string */
export function userInitials(user: { full_name?: string | null; username?: string }): string {
  return (user.full_name?.trim() || user.username || '')
    .split(/\s+/)
    .filter(Boolean)
    .map((n) => n[0])
    .join('')
    .toUpperCase()
    .slice(0, 2)
}

export default function UserMenu({
  onShowTour,
  setupLeft,
}: {
  onShowTour: () => void
  /** open setup steps from the shell's read; null hides the count */
  setupLeft: number | null
}) {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const { screen } = useParams<{ screen?: string }>()
  const { scheme, setScheme } = useColorScheme()
  const [open, setOpen] = useState(false)
  const [version, setVersion] = useState('')
  const [twoStepOpen, setTwoStepOpen] = useState(false)
  const [authBypassed, setAuthBypassed] = useState(BUILT_WITH_BYPASS)
  const [passwordOpen, setPasswordOpen] = useState(false)
  const avatarRef = useRef<HTMLButtonElement>(null)
  const ref = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null)

  const place = useCallback(() => {
    const el = ref.current
    if (!el) return
    const r = el.getBoundingClientRect()
    setPos({ top: r.bottom + 6, right: Math.max(8, window.innerWidth - r.right) })
  }, [])

  useEffect(() => {
    let live = true
    consoleApi
      .getHealth()
      .then((res) => {
        const body = res.data as { version?: string; auth_bypassed?: boolean } | undefined
        if (live && body?.version) setVersion(body.version)
        if (live && body?.auth_bypassed === true) setAuthBypassed(true)
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    if (!open) return
    place()
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node
      if (ref.current?.contains(t) || menuRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open, place])

  if (!user) return null

  // /auth/me can send null/omitted full_name even though the TS type is string
  const displayName = user.full_name?.trim() || user.username || ''
  const initials = userInitials(user)
  const role = user.role_id.replace(/^role-/, '').replace(/-/g, ' ')
  const current = screen || 'console'

  const handleLogout = async () => {
    setOpen(false)
    await logout()
    navigate('/login')
  }

  const openPasswordDialog = () => {
    setOpen(false)
    // the menu item unmounts with the menu; the dialog returns focus to the avatar
    avatarRef.current?.focus()
    setPasswordOpen(true)
  }

  const handlePasswordChanged = async () => {
    setPasswordOpen(false)
    await logout()
    navigate('/login')
  }

  const root = ref.current?.closest('.soc-console') as HTMLElement | null
  const menu =
    open && pos ? (
      <div
        ref={menuRef}
        className="user-pop"
        role="menu"
        aria-label="Account"
        style={{ position: 'fixed', top: pos.top, right: pos.right, zIndex: 80 }}
      >
        <div className="user-pop-head">
          <span className="user-pop-initials" aria-hidden="true">{initials}</span>
          <span className="user-pop-who">
            <span className="user-pop-name">{displayName}</span>
            <span className="user-pop-sub">
              <span className="user-pop-role">{role}</span>
              {user.mfa_enabled ? ' · MFA enabled' : ''}
            </span>
          </span>
        </div>
        <div className="user-pop-sep" />
        <button role="menuitem" onClick={() => { setOpen(false); navigate('/setup') }}>
          <Icon name="flow" size={15} />
          Setup guide
          {setupLeft ? <span className="user-pop-note">{setupLeft} left</span> : null}
        </button>
        <button role="menuitem" onClick={() => { setOpen(false); onShowTour() }}>
          <Icon name="play" size={15} /> Take the tour
        </button>
        <a role="menuitem" href={feedbackHref(current, version)} target="_blank" rel="noreferrer">
          <Icon name="note" size={15} />
          Share feedback
          <span className="user-pop-note">About Vigil{version ? ` · ${version}` : ''}</span>
        </a>
        <button role="menuitem" onClick={() => setScheme(scheme === 'dark' ? 'light' : 'dark')}>
          <Icon name={scheme === 'dark' ? 'sun' : 'moon'} size={15} />
          {scheme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        </button>
        <button role="menuitem" onClick={() => { setOpen(false); setTwoStepOpen(true) }}>
          <Icon name="shield" size={15} />
          Two-step sign-in
          <span className="user-pop-note">{user.mfa_enabled ? 'On' : 'Off'}</span>
        </button>
        <button role="menuitem" onClick={() => { setOpen(false); navigate('/settings') }}>
          <Icon name="gear" size={15} /> Settings
        </button>
        {!authBypassed && (
          <button role="menuitem" onClick={openPasswordDialog}>
            <Icon name="lock" size={15} /> Change password
          </button>
        )}
        <button role="menuitem" className="signout" onClick={handleLogout}>
          <Icon name="logout" size={15} /> Sign out
        </button>
      </div>
    ) : null

  const twoStep = <TwoStepDialog open={twoStepOpen} onClose={() => setTwoStepOpen(false)} />
  const dialog = (
    <ChangePasswordDialog
      open={passwordOpen}
      onClose={() => setPasswordOpen(false)}
      onSignedOut={handlePasswordChanged}
    />
  )

  return (
    <div className="user-menu" ref={ref}>
      <button
        ref={avatarRef}
        className={`vg-avatar${open ? ' active' : ''}`}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
      >
        <span>{initials}</span>
      </button>
      {menu && (root ? createPortal(menu, root) : menu)}
      {root ? createPortal(twoStep, root) : twoStep}
      {passwordOpen && (root ? createPortal(dialog, root) : dialog)}
    </div>
  )
}
