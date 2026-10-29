import { useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { CasesDetail } from '../screens/cases/CasesScreen'

/** The case page under the header. Expand opens it full page at /cases?case=, noting where it came from. */
export default function CaseDrawer({
  caseId,
  onClose,
  pageKey,
  seed,
  onSeedConsumed,
}: {
  caseId: string
  onClose: () => void
  /** SocConsole's current route key, stored on the pinned thread as page_context. */
  pageKey: string
  seed?: string | null
  onSeedConsumed?: () => void
}) {
  const navigate = useNavigate()
  const location = useLocation()

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="vg-drawer-scrim" onMouseDown={onClose}>
      <aside
        className="vg-case-drawer"
        role="dialog"
        aria-label="Case"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <CasesDetail
          id={caseId}
          onBack={onClose}
          onExpand={() =>
            // remember the page the panel was open on, so the full page can hand the case back to it
            navigate(
              { pathname: '/cases', search: `?case=${encodeURIComponent(caseId)}` },
              { state: { origin: { pathname: location.pathname, search: location.search } } },
            )
          }
          pageKey={pageKey}
          seed={seed}
          onSeedConsumed={onSeedConsumed}
        />
      </aside>
    </div>
  )
}
