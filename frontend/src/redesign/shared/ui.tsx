/* ============================================================
   Shared redesign UI primitives — a modal Popup and a Dropdown
   menu. Both are scoped under .soc-console (no portal, so the
   dark theme styles apply) and carry the a11y affordances the
   raw-div redesign otherwise lacks (Esc, focus return, outside
   click, role/aria). See REDESIGN_GAPS.md §3, §10.
   ============================================================ */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type InputHTMLAttributes,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { Icon } from './icons'

/** Enter/Space → activate. For non-button elements given `role="button"` or
 *  `role="switch"` so they stay keyboard-operable (REDESIGN_GAPS.md §10). */
export function activateOnKey(fn: () => void) {
  return (e: ReactKeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      fn()
    }
  }
}

/* ---------------- Popup (modal dialog) ---------------- */
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
  width?: number
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  // Keep the latest onClose in a ref so the focus effect can depend on `open`
  // alone. Depending on `onClose` (usually a fresh inline arrow each render)
  // would re-run the effect on every keystroke and steal focus back to the
  // panel — making inputs inside the modal accept only one character.
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return
    const opener = document.activeElement as HTMLElement | null
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    document.addEventListener('keydown', onKey)
    panelRef.current?.focus()
    return () => {
      document.removeEventListener('keydown', onKey)
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
        aria-label={typeof title === 'string' ? title : 'Dialog'}
        tabIndex={-1}
        style={{ width }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="modal-x" title="Close" onClick={onClose}><Icon name="close" size={16} /></button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  )
}

/* ---------------- Filter button + popover ---------------- */
export interface DropOption {
  value: string
  label: string
}

/** a single "Filters" button that opens a popover holding the filter groups */
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

/** one labelled row of selectable option chips inside the filter popover */
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

/* ---------------- Form-field select (styled <select> replacement) ----------------
   A dark, on-brand dropdown for use inside forms/dialogs where the native
   <select> popup (light, OS-chrome) looks out of place. Same a11y wiring as
   Dropdown but rendered as a full-width field matching inputCls. */
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
  // Menu is rendered in a fixed-position portal so it escapes any overflow
  // clipping (cards, .table-wrap, the scrolling settings pane). We anchor it
  // to the trigger's bounding rect and reposition on scroll/resize.
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
    window.addEventListener('scroll', place, true) // capture: any scroll container
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
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
        // zIndex above the modal overlay (70) so Selects inside a Popup aren't hidden
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

/* ---------------- Form primitives (settings forms) ----------------
   Dark, on-brand inputs/toggles matching the redesign tokens. Built for
   the Settings screen but generic enough for any redesign form. */

/** label + control + hint wrapper. Wrap a control as children. */
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

/** single-line text input styled to match .field-select */
export function TextInput({ className = '', ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`field-input ${className}`.trim()} {...props} />
}

/** numeric input — same styling, type=number */
export function NumberInput({ className = '', ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input type="number" className={`field-input ${className}`.trim()} {...props} />
}

/** password input with a reveal toggle */
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

/** on/off switch (role=switch) */
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

/** a labelled toggle row (switch on the right, text on the left) */
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

/* ---------------- Rating (stars) + Slider ----------------
   The icon set is stroke-only (no fillable star), so Rating inlines its
   own star glyph; Slider themes a native range input via accent-color.
   Both are bare controls — wrap in <Field label="…"> for a visible label.
   Added for the AI Decisions detailed-feedback Popup (DECISIONS_WIRING.md §6). */

/** filled / outline star — fill is controlled (the shared Icon forces fill=none) */
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

/** 1..max star rating; `value` 0 means unrated. Hover previews the score. */
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

/** themed range slider with a live value readout on the right */
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

/** card container for a settings group — reuses .card/.card-h/.card-b.
   `wide` opts out of the default content max-width (for wide tables). */
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

/** confirmation dialog for destructive actions — wraps Popup */
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
  /** prefix shown before the value, e.g. "Severity" */
  label: string
  value: string
  options: DropOption[]
  onSelect: (value: string) => void
  /** whether to render in the highlighted (active) chip style */
  selected?: boolean
  /** when active, show an ✕ that clears the filter */
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
