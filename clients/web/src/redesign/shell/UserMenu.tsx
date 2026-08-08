/* ============================================================
   Account menu for the nav rail — an avatar button that opens a
   dropdown (name / email / role, Settings, Logout). Reads the
   session from AuthContext (DEV_MODE seeds a full-admin dev user,
   so this renders in the preview too). Behaviour ported from
   components/auth/UserMenu.tsx, restyled for the MUI-free
   .soc-console shell. See REDESIGN_GAPS.md §2.
   ============================================================ */
import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { Icon } from '../shared/icons'

export default function UserMenu() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  // The avatar sits at the bottom-left of the viewport, so the menu is
  // portaled with fixed coords rising from the button: anchored left of the
  // rail and bottom-aligned to the trigger (mirrors the Select primitive).
  const [pos, setPos] = useState<{ left: number; bottom: number } | null>(null)

  const place = useCallback(() => {
    const el = ref.current
    if (!el) return
    const r = el.getBoundingClientRect()
    setPos({ left: r.right + 8, bottom: window.innerHeight - r.bottom })
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

  const initials = user.full_name
    .split(' ')
    .map((n) => n[0])
    .join('')
    .toUpperCase()
    .slice(0, 2)
  const role = user.role_id.replace(/^role-/, '').replace(/-/g, ' ')

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
        style={{ position: 'fixed', left: pos.left, bottom: pos.bottom, zIndex: 80 }}
      >
        <div className="user-pop-head">
          <div className="user-pop-name">{user.full_name}</div>
          <div className="user-pop-email">{user.email}</div>
          <div className="user-pop-role">Role: {role}</div>
        </div>
        <div className="user-pop-sep" />
        <button role="menuitem" onClick={() => { setOpen(false); navigate('/settings') }}>
          <Icon name="gear" size={15} /> Settings
        </button>
        {user.mfa_enabled && (
          <div className="user-pop-mfa"><Icon name="shield" size={15} /> MFA enabled</div>
        )}
        <div className="user-pop-sep" />
        <button role="menuitem" className="danger" onClick={handleLogout}>
          <Icon name="logout" size={15} /> Logout
        </button>
      </div>
    ) : null

  return (
    <div className="user-menu" ref={ref}>
      <button
        className={`nav-btn user-btn${open ? ' active' : ''}`}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
      >
        <span className="avatar">{initials}</span>
        <span className="nav-label">{user.full_name}</span>
        <span className="tip">{user.full_name}</span>
      </button>
      {menu && (root ? createPortal(menu, root) : menu)}
    </div>
  )
}
