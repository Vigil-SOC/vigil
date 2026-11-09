import { useState } from 'react'
import { errorText } from '../../shared/errorText'
import { detectionRulesApi } from '../../services/api'

/** "Deploy again" for a live copy whose deploy PR was closed unmerged (#4123): a
 * new PR for its current version. What came of it replaces the button. */
export default function DeployAgain({ ruleId, onDone, className }: { ruleId: number; onDone: () => void; className?: string }) {
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<{ text: string; href?: string; bad?: boolean } | null>(null)
  const go = () => {
    setBusy(true)
    detectionRulesApi.deployOverlayOnly([ruleId])
      .then((res) => {
        const { opened, failed, skipped, asked } = res.data
        const pr = opened.find((o) => o.rule_id === ruleId)
        const num = pr?.pr_url ? /\/pull\/(\d+)/.exec(pr.pr_url)?.[1] : undefined
        if (pr?.pr_url) setSaid({ text: num ? `Opened PR #${num}` : 'Opened a new PR', href: pr.pr_url })
        else if (pr?.in_repo) setSaid({ text: `Already in ${pr.in_repo}` })
        else if (asked?.some((a) => a.rule_id === ruleId)) setSaid({ text: 'Waiting for an approval' })
        else setSaid({ text: failed[0]?.reason || skipped[0]?.reason || 'Nothing was opened', bad: true })
        onDone()
      })
      .catch((e) => setSaid({ text: errorText(e, 'Deploying again failed.'), bad: true }))
      .finally(() => setBusy(false))
  }
  if (said) {
    return (
      <span className={className} role="status">
        {said.href ? <a className="de-link" href={said.href} target="_blank" rel="noreferrer">{said.text}</a> : <span className={said.bad ? 'de-x-err' : undefined}>{said.text}</span>}
      </span>
    )
  }
  return (
    <button type="button" className={`de-link ${className ?? ''}`} disabled={busy} title="Open a new deploy PR for its current version" onClick={go}>
      {busy ? 'Deploying…' : 'Deploy again'}
    </button>
  )
}
