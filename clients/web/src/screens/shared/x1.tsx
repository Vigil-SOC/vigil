import type { ReactNode } from 'react'
import './x1.css'

export function Failure({ children }: { children: ReactNode }) {
  return <div className="x1-card x1-error" role="alert">{children}</div>
}

export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="x1-card" style={{ display: 'grid', gap: 6 }}>
      <b style={{ color: 'var(--tx0)', fontSize: 13.5 }}>{title}</b>
      <span className="x1-muted" style={{ fontSize: 12.5 }}>{children}</span>
    </div>
  )
}
