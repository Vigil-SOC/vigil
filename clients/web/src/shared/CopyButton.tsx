import { useEffect, useState } from 'react'
import { Icon } from './icons'

/** A small copy icon beside a value. Copies `text` and shows a check for a moment; `label` names what is copied ("case id"). */
export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(timer)
  }, [copied])
  const name = copied ? `Copied ${label}` : `Copy ${label}`
  return (
    <button
      type="button"
      className="btn ghost icon"
      aria-label={name}
      title={name}
      aria-live="polite"
      onClick={(event) => {
        // Inside a clickable row or header the copy is the only thing the click does.
        event.stopPropagation()
        void navigator.clipboard?.writeText(text).then(() => setCopied(true), () => {})
      }}
    >
      <Icon name={copied ? 'check' : 'copy'} size={13} />
    </button>
  )
}
