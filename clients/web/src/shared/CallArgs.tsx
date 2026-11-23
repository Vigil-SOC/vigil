import './approval-call.css'
import { visibleText } from './visibleText'

/**
 * A gated call's tool and the exact arguments approving it releases: in full and verbatim
 * (not re-serialised, which could change numbers or duplicate keys), hidden characters shown.
 */
export function CallArgs({ tool, args }: { tool?: string | null; args?: string | null }) {
  if (!tool && args == null) return null
  return (
    <div className="approval-call">
      <span className="approval-call-label">{tool ? <>Calls <code>{visibleText(tool)}</code> with</> : 'With'}</span>
      <pre aria-label="Arguments approving releases">{args == null || args === '' ? '(no arguments)' : visibleText(args)}</pre>
    </div>
  )
}
