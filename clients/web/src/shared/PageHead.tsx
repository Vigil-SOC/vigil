import type { ReactNode } from 'react'
import './kit.css'

/**
 * Title and one-line description at the top of a screen; `actions` sit at the
 * right of the head. A screen that owns its page heading passes `level="h1"`.
 */
export function PageHead({
  title,
  description,
  actions,
  level: Heading = 'h2',
}: {
  title: string
  description: string
  actions?: ReactNode
  level?: 'h1' | 'h2'
}) {
  if (!actions) {
    return (
      <header className="page-head">
        <Heading>{title}</Heading>
        <p>{description}</p>
      </header>
    )
  }
  return (
    <header className="page-head with-actions">
      <div className="page-head-text">
        <Heading>{title}</Heading>
        <p>{description}</p>
      </div>
      <div className="page-head-actions">{actions}</div>
    </header>
  )
}
