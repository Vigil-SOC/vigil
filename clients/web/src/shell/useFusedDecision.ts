import { useCallback } from 'react'
import { approvalsApi, type NeedsYouItem } from '../services/api'
import { useToast } from './toast'

/**
 * A reversible approve and every reject of a Needs-you item, through the toast's undo fuse.
 * Home's card and the case page's decision block both call this, so one gate behaves one way:
 * the toast owns the call, so it survives leaving the screen, and Undo sends nothing.
 */
export function useFusedDecision(failText: (err: unknown) => string) {
  const { notifyUndoable } = useToast()

  const fuse = useCallback(
    (item: NeedsYouItem, verb: string, past: string, commit: () => Promise<unknown>) =>
      notifyUndoable({
        key: item.source_id,
        text: `${verb}: ${item.title}`,
        commit,
        doneText: `${past}: ${item.title}`,
        failText,
      }),
    [notifyUndoable, failText],
  )

  const approve = useCallback(
    (item: NeedsYouItem) => fuse(item, 'Approving', 'Approved', () => approvalsApi.approve(item.source_id)),
    [fuse],
  )

  const reject = useCallback(
    (item: NeedsYouItem, reason: string) => fuse(item, 'Rejecting', 'Rejected', () => approvalsApi.reject(item.source_id, reason)),
    [fuse],
  )

  return { approve, reject }
}
