import './kit.css'

export interface TabItem<T extends string> {
  id: T
  label: string
  count?: number
}

/** Underlined tabs with count chips. Sits on its container's 1px bottom border. */
export function TabStrip<T extends string>({
  tabs,
  active,
  onChange,
  label,
  className,
}: {
  tabs: readonly TabItem<T>[]
  active: T
  onChange: (id: T) => void
  label: string
  className?: string
}) {
  return (
    <div className={`tab-strip${className ? ` ${className}` : ''}`} role="tablist" aria-label={label}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          aria-selected={active === tab.id}
          className={active === tab.id ? 'active' : undefined}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
          {tab.count != null && <span className="tab-count">{tab.count}</span>}
        </button>
      ))}
    </div>
  )
}
