/* Scoped under .soc-console (no portal, so the dark theme styles apply) and
   carrying the a11y affordances raw divs lack: Esc, focus return, outside
   click, role/aria. */
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type InputHTMLAttributes,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { Icon, type IconName } from './icons'

// Popup and portal-backed Select nest, so Escape is scoped to the most recently
// opened layer — one keypress must not dismiss both.
const escapeLayers: symbol[] = []

function addEscapeLayer(layer: symbol) {
  escapeLayers.push(layer)
}

function removeEscapeLayer(layer: symbol) {
  const index = escapeLayers.lastIndexOf(layer)
  if (index >= 0) escapeLayers.splice(index, 1)
}

function isTopEscapeLayer(layer: symbol) {
  return escapeLayers[escapeLayers.length - 1] === layer
}

/** keeps non-button elements given role="button"/"switch" keyboard-operable */
export function activateOnKey(fn: () => void) {
  return (e: ReactKeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      fn()
    }
  }
}

export function EmptyState({
  icon = 'info',
  title,
  body,
  primary,
  secondary,
  compact = false,
  table = false,
  loading = false,
  error = false,
}: {
  icon?: IconName
  title: ReactNode
  body?: ReactNode
  primary?: { label: ReactNode; onClick: () => void; icon?: IconName }
  secondary?: { label: ReactNode; onClick: () => void; icon?: IconName }
  compact?: boolean
  table?: boolean
  /** live region, so screen readers hear the transition */
  loading?: boolean
  error?: boolean
}) {
  const content = (
    <div
      className={`empty-state${compact ? ' compact' : ''}`}
      role={loading ? 'status' : error ? 'alert' : undefined}
      aria-live={loading ? 'polite' : error ? 'assertive' : undefined}
    >
      <div className="empty-state-icon"><Icon name={icon} size={compact ? 18 : 24} /></div>
      <div className="empty-state-copy">
        <h3>{title}</h3>
        {body && <p>{body}</p>}
      </div>
      {(primary || secondary) && (
        <div className="empty-state-actions">
          {secondary && (
            <button className="btn ghost" onClick={secondary.onClick}>
              {secondary.icon && <Icon name={secondary.icon} />}
              {secondary.label}
            </button>
          )}
          {primary && (
            <button className="btn primary" onClick={primary.onClick}>
              {primary.icon && <Icon name={primary.icon} />}
              {primary.label}
            </button>
          )}
        </div>
      )}
    </div>
  )
  return table ? <div className={`empty-state-table${compact ? ' compact' : ''}`}>{content}</div> : content
}

export function Popup({
  open,
  onClose,
  title,
  children,
  width = 560,
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  children: ReactNode
  // A number is pixels; a string is any CSS width. .modal's max-width keeps it inside
  // the viewport either way.
  width?: number | string
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  const escapeLayerRef = useRef(Symbol('popup'))
  const titleId = useId()
  // onClose is usually a fresh inline arrow each render; depending on it would
  // re-run the focus effect on every keystroke and steal focus back to the panel,
  // so inputs inside the modal would accept only one character.
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return
    const escapeLayer = escapeLayerRef.current
    addEscapeLayer(escapeLayer)
    const opener = document.activeElement as HTMLElement | null
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isTopEscapeLayer(escapeLayer)) {
        e.preventDefault()
        e.stopImmediatePropagation()
        onCloseRef.current()
      }
    }
    document.addEventListener('keydown', onKey)
    panelRef.current?.focus()
    return () => {
      document.removeEventListener('keydown', onKey)
      removeEscapeLayer(escapeLayer)
      opener?.focus?.()
    }
  }, [open])

  if (!open) return null
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        ref={panelRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        style={{ width }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h3 id={titleId}>{title}</h3>
          <button className="modal-x" title="Close" aria-label="Close" onClick={onClose}><Icon name="close" size={16} /></button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  )
}

export interface DropOption {
  value: string
  label: string
}

export function FilterButton({
  activeCount,
  onClearAll,
  children,
}: {
  activeCount: number
  onClearAll?: () => void
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="drop" ref={ref}>
      <button
        type="button"
        className={`btn ghost${activeCount > 0 ? ' has-filters' : ''}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon name="filter" /> Filters
        {activeCount > 0 && <span className="filter-badge">{activeCount}</span>}
      </button>
      {open && (
        <div className="filter-pop" role="dialog" aria-label="Filters">
          <div className="filter-pop-head">
            <span className="filter-pop-title"><Icon name="filter" size={13} /> Filters</span>
            {activeCount > 0 && onClearAll && (
              <button className="filter-clear-all" onClick={onClearAll}>Clear all</button>
            )}
          </div>
          {children}
        </div>
      )}
    </div>
  )
}

export function FilterGroup({
  label,
  value,
  options,
  onSelect,
}: {
  label: string
  value: string
  options: DropOption[]
  onSelect: (value: string) => void
}) {
  return (
    <div className="filter-grp">
      <span className="filter-grp-label">{label}</span>
      <div className="filter-opts">
        {options.map((o) => (
          <button
            key={o.value}
            className={`filter-opt${o.value === value ? ' on' : ''}`}
            onClick={() => onSelect(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  )
}

export function Select({
  value,
  options,
  onSelect,
  placeholder = 'Select…',
}: {
  value: string
  options: DropOption[]
  onSelect: (value: string) => void
  placeholder?: string
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const escapeLayerRef = useRef(Symbol('select'))
  // a fixed-position portal, to escape overflow clipping (cards, .table-wrap,
  // the scrolling settings pane); anchored to the trigger's rect
  const [pos, setPos] = useState<{ left: number; top: number; width: number } | null>(null)
  const current = options.find((o) => o.value === value)

  const place = useCallback(() => {
    const el = ref.current
    if (!el) return
    const r = el.getBoundingClientRect()
    setPos({ left: r.left, top: r.bottom + 4, width: r.width })
  }, [])

  useEffect(() => {
    if (!open) return
    const escapeLayer = escapeLayerRef.current
    addEscapeLayer(escapeLayer)
    place()
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node
      if (ref.current?.contains(t) || menuRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isTopEscapeLayer(escapeLayer)) {
        e.preventDefault()
        e.stopImmediatePropagation()
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true) // capture: any scroll container
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
      removeEscapeLayer(escapeLayer)
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open, place])

  const root = ref.current?.closest('.soc-console') as HTMLElement | null
  const menu =
    open && pos ? (
      <div
        ref={menuRef}
        className="drop-menu field-menu"
        role="listbox"
        // above the modal overlay (70), so a Select inside a Popup isn't hidden
        style={{ position: 'fixed', left: pos.left, top: pos.top, width: pos.width, minWidth: pos.width, right: 'auto', zIndex: 80 }}
      >
        {options.map((o) => (
          <button
            key={o.value}
            role="option"
            aria-selected={o.value === value}
            className={o.value === value ? 'sel' : ''}
            onClick={() => {
              onSelect(o.value)
              setOpen(false)
            }}
          >
            {o.label}
          </button>
        ))}
      </div>
    ) : null

  return (
    <div className="drop field-drop" ref={ref}>
      <button
        type="button"
        className="field-select"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={current ? '' : 'text-tx-3'}>{current?.label ?? placeholder}</span>
        <span className="dd"><Icon name="chevD" size={13} /></span>
      </button>
      {menu && (root ? createPortal(menu, root) : menu)}
    </div>
  )
}

export function Field({
  label,
  hint,
  error,
  children,
}: {
  label?: ReactNode
  hint?: ReactNode
  error?: string | null
  children: ReactNode
}) {
  return (
    <label className="field">
      {label && <span className="field-label">{label}</span>}
      {children}
      {error ? (
        <span className="field-hint err">{error}</span>
      ) : (
        hint && <span className="field-hint">{hint}</span>
      )}
    </label>
  )
}

export function TextInput({ className = '', ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`field-input ${className}`.trim()} {...props} />
}

export function NumberInput({ className = '', ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input type="number" className={`field-input ${className}`.trim()} {...props} />
}

export function PasswordInput({ className = '', ...props }: InputHTMLAttributes<HTMLInputElement>) {
  const [show, setShow] = useState(false)
  return (
    <span className="field-input-wrap">
      <input
        type={show ? 'text' : 'password'}
        className={`field-input has-affix ${className}`.trim()}
        {...props}
      />
      <button
        type="button"
        className="field-affix"
        aria-label={show ? 'Hide value' : 'Reveal value'}
        onClick={() => setShow((s) => !s)}
      >
        <Icon name={show ? 'lock' : 'eye'} size={14} />
      </button>
    </span>
  )
}

export function Toggle({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
  label?: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={`toggle${checked ? ' on' : ''}`}
      onClick={() => !disabled && onChange(!checked)}
    >
      <span className="toggle-knob" />
    </button>
  )
}

export function ToggleRow({
  label,
  hint,
  checked,
  onChange,
  disabled,
}: {
  label: ReactNode
  hint?: ReactNode
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
}) {
  return (
    <div className="toggle-row">
      <div className="toggle-row-text">
        <span className="toggle-row-label">{label}</span>
        {hint && <span className="toggle-row-hint">{hint}</span>}
      </div>
      <Toggle checked={checked} onChange={onChange} disabled={disabled} />
    </div>
  )
}

/** the shared Icon forces fill=none, so fill is controlled here */
function Star({ filled, size }: { filled: boolean; size: number }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill={filled ? 'var(--accent)' : 'none'}
      stroke={filled ? 'var(--accent)' : 'var(--tx-3)'}
      strokeWidth={1.6}
      strokeLinejoin="round"
    >
      <path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z" />
    </svg>
  )
}

export function Rating({
  value,
  onChange,
  max = 5,
  size = 22,
  label,
}: {
  value: number
  onChange: (v: number) => void
  max?: number
  size?: number
  label?: string
}) {
  const [hover, setHover] = useState(0)
  const active = hover || value
  return (
    <div
      className="rating"
      role="radiogroup"
      aria-label={label}
      style={{ display: 'inline-flex', gap: 4 }}
    >
      {Array.from({ length: max }, (_, i) => i + 1).map((n) => (
        <button
          key={n}
          type="button"
          role="radio"
          aria-checked={value === n}
          aria-label={`${n} of ${max}`}
          onClick={() => onChange(n)}
          onMouseEnter={() => setHover(n)}
          onMouseLeave={() => setHover(0)}
          style={{
            background: 'none',
            border: 0,
            padding: 2,
            cursor: 'pointer',
            lineHeight: 0,
          }}
        >
          <Star filled={n <= active} size={size} />
        </button>
      ))}
    </div>
  )
}

export function Slider({
  value,
  onChange,
  min = 0,
  max = 100,
  step = 1,
  label,
  format,
}: {
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  step?: number
  label?: string
  format?: (v: number) => string
}) {
  return (
    <div className="slider-row" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        aria-label={label}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ flex: 1, accentColor: 'var(--accent)', cursor: 'pointer' }}
      />
      <span
        className="mono"
        style={{ minWidth: 64, textAlign: 'right', color: 'var(--tx-2)', fontSize: 13 }}
      >
        {format ? format(value) : value}
      </span>
    </div>
  )
}

/** `wide` opts out of the default content max-width, for wide tables */
export function SettingsCard({
  title,
  desc,
  actions,
  wide,
  children,
}: {
  title: ReactNode
  desc?: ReactNode
  actions?: ReactNode
  wide?: boolean
  children: ReactNode
}) {
  return (
    <section className={`card card-sq settings-card${wide ? ' wide' : ''}`}>
      <div className="card-h">
        <div className="settings-card-head">
          <h3>{title}</h3>
          {desc && <p>{desc}</p>}
        </div>
        {actions && (
          <>
            <span className="grow" />
            <div className="settings-card-actions">{actions}</div>
          </>
        )}
      </div>
      <div className="card-b">{children}</div>
    </section>
  )
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = 'Confirm',
  danger = true,
  busy = false,
  onConfirm,
  onClose,
}: {
  open: boolean
  title: ReactNode
  body: ReactNode
  confirmLabel?: string
  danger?: boolean
  busy?: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  return (
    <Popup open={open} onClose={onClose} title={title} width={440}>
      <p className="text-sm text-tx-2 leading-relaxed">{body}</p>
      <div className="flex justify-end gap-2.5 mt-5">
        <button className="btn ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button
          className={`btn ${danger ? 'danger' : 'primary'}`}
          onClick={onConfirm}
          disabled={busy}
        >
          {busy ? 'Working…' : confirmLabel}
        </button>
      </div>
    </Popup>
  )
}

export function Dropdown({
  label,
  value,
  options,
  onSelect,
  selected,
  onClear,
}: {
  label: string
  value: string
  options: DropOption[]
  onSelect: (value: string) => void
  selected?: boolean
  onClear?: () => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const current = options.find((o) => o.value === value)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="drop" ref={ref}>
      <button
        type="button"
        className={`chip${selected ? ' sel' : ''}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {label}: {current?.label ?? value}
        {selected && onClear ? (
          <span
            className="dd clear"
            role="button"
            tabIndex={0}
            aria-label={`Clear ${label} filter`}
            onClick={(e) => { e.stopPropagation(); onClear() }}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); onClear() } }}
          >
            <Icon name="close" size={11} />
          </span>
        ) : (
          <span className="dd"><Icon name="chevD" size={12} /></span>
        )}
      </button>
      {open && (
        <div className="drop-menu" role="listbox">
          {options.map((o) => (
            <button
              key={o.value}
              role="option"
              aria-selected={o.value === value}
              className={o.value === value ? 'sel' : ''}
              onClick={() => {
                onSelect(o.value)
                setOpen(false)
              }}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
