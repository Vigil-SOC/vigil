import { useEffect, useState } from 'react'
import { OverlayPortal } from '../../shared/ui'
import type { Skill } from '../../data/appData'
import { skillsManagedText } from '../../services/skillsApi'
import { SkillDeleteModal } from './SkillDrawer'
import { SkillLibraryDialog } from './SkillLibraryDialog'

// The side panel for the skills installed from one repo: where they came from,
// update or delete them together, and open any one in its own drawer.
export function SkillLibraryPanel({
  skills,
  canWrite,
  onClose,
  onOpenSkill,
  onChanged,
}: {
  skills: Skill[]
  canWrite: boolean
  onClose: () => void
  onOpenSkill: (name: string) => void
  onChanged: () => void
}) {
  const [updating, setUpdating] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const first = skills[0]
  const lib = first.library!
  const held = skills.filter((s) => s.held).length
  const managedText = first.managed ? skillsManagedText(first.managed) : undefined

  // The update and delete popups take their own Escape; the panel closes only when neither is open.
  useEffect(() => {
    if (updating || deleting) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, updating, deleting])

  return (
    <OverlayPortal>
      <div className="vg-side-scrim" onMouseDown={onClose}>
        <aside className="vg-side-panel" role="dialog" aria-label={`Library ${lib.label}`} onMouseDown={(e) => e.stopPropagation()}>
          <div className="flex items-start gap-3">
            <span className="flex flex-col gap-[3px] grow min-w-0">
              <span className="text-[20px] font-bold leading-[1.25] tracking-[-0.2px] text-tx break-words">Library · {lib.label}</span>
              <span className="vg-side-hint">From {lib.label} · commit {lib.commit.slice(0, 7)}{held > 0 ? ` · ${held} held` : ''}</span>
            </span>
            <button type="button" className="vg-side-close" aria-label="Close" onClick={onClose}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />
              </svg>
            </button>
          </div>
          <div className="vg-side-field">
            <span className="vg-skill-files-title">{skills.length} skills</span>
            {skills.map((s) => (
              <button key={s.name} type="button" className="vg-skill-file" onClick={() => onOpenSkill(s.name)}>
                <span className="min-w-0 break-words grow">{s.name}</span>
                {s.held && <span className="text-[11px] font-bold shrink-0" style={{ color: 'var(--crit)' }}>Held</span>}
              </button>
            ))}
          </div>
          {canWrite && managedText && <p className="text-[13px] text-tx-2 leading-[1.5] break-words">{managedText}</p>}
          <div className="vg-side-foot">
            {canWrite && (
              <button type="button" className="vg-side-btn" disabled={!!managedText} title={managedText} onClick={() => setUpdating(true)}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M20 11a8 8 0 1 0-2.3 5.7M20 4.5V11h-6.5" />
                </svg>
                Update library
              </button>
            )}
            <span className="vg-skill-foot-end">
              <button type="button" className="vg-side-btn" onClick={onClose}>Close</button>
              {canWrite && (
                <button type="button" className="vg-side-btn" disabled={!!managedText} title={managedText} onClick={() => setDeleting(true)}>Delete library</button>
              )}
            </span>
          </div>
        </aside>
      </div>
      {updating && <SkillLibraryDialog name={first.name} label={lib.label} onClose={() => setUpdating(false)} onUpdated={onChanged} />}
      {deleting && <SkillDeleteModal skill={first} wholeLibrary onClose={() => setDeleting(false)} onDeleted={onChanged} />}
    </OverlayPortal>
  )
}
