import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { CasesDetail } from '../screens/cases/CasesScreen'

/** The case page under the header. Expand opens it full page at /cases?case=. */
export default function CaseDrawer({
  caseId,
  onClose,
  pageKey,
}: {
  caseId: string
  onClose: () => void
  /** SocConsole's current route key, stored on the pinned thread as page_context. */
  pageKey: string
}) {
  const navigate = useNavigate()

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
          onExpand={() => {
            navigate({ pathname: '/cases', search: `?case=${encodeURIComponent(caseId)}` })
            onClose()
          }}
          pageKey={pageKey}
        />
      </aside>
    </div>
  )
}
