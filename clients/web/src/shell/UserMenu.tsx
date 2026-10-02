import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate, useParams } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { consoleApi } from '../services/api'
import { Icon } from '../shared/icons'
import { useSocTheme } from './theme'

const FEEDBACK_BASE =
  import.meta.env.VITE_FEEDBACK_URL ||
  'https://github.com/Vigil-SOC/vigil/issues/new?labels=console-feedback'

function feedbackHref(screen: string, version: string): string {
  const url = new URL(FEEDBACK_BASE)
  url.searchParams.set('title', `Console feedback: ${screen}`)
  url.searchParams.set('body', `Screen: ${screen}\nVersion: ${version || 'unknown'}\n\n`)
  return url.toString()
}

export default function UserMenu({ onShowTour }: { onShowTour: () => void }) {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const { screen } = useParams<{ screen?: string }>()
  const { scheme, setBgPreset } = useSocTheme()
  const [open, setOpen] = useState(false)
  const [version, setVersion] = useState('')
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
        const value = (res.data as { version?: string } | undefined)?.version
        if (live && value) setVersion(value)
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
  const initials = displayName
    .split(/\s+/)
    .filter(Boolean)
    .map((n) => n[0])
    .join('')
    .toUpperCase()
    .slice(0, 2)
  const role = user.role_id.replace(/^role-/, '').replace(/-/g, ' ')
  const current = screen || 'console'

  const handleLogout = async () => {
    setOpen(false)
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
          <div className="user-pop-name">{displayName}</div>
          <div className="user-pop-email">{user.email}</div>
          <div className="user-pop-role">Role: {role}</div>
        </div>
        <div className="user-pop-sep" />
        <div className="user-pop-about">About{version ? ` · ${version}` : ''}</div>
        <a role="menuitem" href={feedbackHref(current, version)} target="_blank" rel="noreferrer">
          Share feedback
        </a>
        <div className="user-pop-sep" />
        <button
          role="menuitem"
          aria-pressed={scheme === 'dark'}
          onClick={() => setBgPreset('slate')}
        >
          <Icon name="moon" size={15} /> Dark
        </button>
        <button
          role="menuitem"
          aria-pressed={scheme === 'light'}
          onClick={() => setBgPreset('light')}
        >
          <Icon name="sun" size={15} /> Light
        </button>
        <div className="user-pop-sep" />
        <button role="menuitem" onClick={() => { setOpen(false); onShowTour() }}>
          <Icon name="info" size={15} /> Console tour
        </button>
        <button role="menuitem" onClick={() => { setOpen(false); navigate('/setup') }}>
          <Icon name="flow" size={15} /> Setup
        </button>
        <button role="menuitem" onClick={() => { setOpen(false); navigate('/settings') }}>
          <Icon name="gear" size={15} /> Settings
        </button>
        {user.mfa_enabled && (
          <div className="user-pop-mfa"><Icon name="shield" size={15} /> MFA enabled</div>
        )}
        <div className="user-pop-sep" />
        <button role="menuitem" className="danger" onClick={handleLogout}>
          <Icon name="logout" size={15} /> Sign out
        </button>
      </div>
    ) : null

  return (
    <div className="user-menu" ref={ref}>
      <button
        className={`vg-avatar${open ? ' active' : ''}`}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
      >
        <span className="avatar">{initials}</span>
      </button>
      {menu && (root ? createPortal(menu, root) : menu)}
    </div>
  )
}
