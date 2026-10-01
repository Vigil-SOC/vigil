import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { CasesDetail } from '../screens/cases/CasesScreen'
import { INITIAL_CASE_FILTERS, useCases } from '../screens/cases/useCases'

/** Case detail under the header. The cases list keeps its own ?case= param. */
export default function CaseDrawer({
  caseId,
  onClose,
  onSelect,
  openChat,
}: {
  caseId: string
  onClose: () => void
  onSelect: (caseId: string) => void
  openChat: (prompt?: string) => void
}) {
  const navigate = useNavigate()
  const { rows, reload } = useCases(INITIAL_CASE_FILTERS)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
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
        <div className="vg-drawer-tools">
          <button
            type="button"
            className="btn ghost"
            onClick={() => {
              navigate({ pathname: '/cases', search: `?case=${encodeURIComponent(caseId)}` })
              onClose()
            }}
          >
            Expand
          </button>
          <button type="button" className="btn ghost" onClick={onClose}>
            Close
          </button>
        </div>
        <CasesDetail
          id={caseId}
          rows={rows}
          onSelect={onSelect}
          onBack={onClose}
          openChat={openChat}
          reloadList={reload}
        />
      </aside>
    </div>
  )
}
