import { useEffect, useId, useRef, type ReactNode } from 'react'
import { Icon } from '../../shared/icons'

export default function FindingsDrawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null)
  const id = useId()
  useEffect(() => {
    const dialog = ref.current
    const opener = document.activeElement
    dialog?.showModal()
    return () => {
      dialog?.close()
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [])
  return <dialog ref={ref} className="findings-drawer" aria-labelledby={id} onCancel={(event) => { event.preventDefault(); onClose() }}>
    <header><h2 id={id}>{title}</h2><button className="btn ghost icon" aria-label={`Close ${title}`} onClick={onClose}><Icon name="close" /></button></header>
    <div className="findings-drawer-body">{children}</div>
  </dialog>
}
