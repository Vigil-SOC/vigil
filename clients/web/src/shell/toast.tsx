import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { Icon } from '../shared/icons'

export type ToastKind = 'ok' | 'err' | 'info'

interface Toast {
  id: number
  kind: ToastKind
  text: string
  /** key of the running undo fuse, if this toast has one */
  fuse?: string
}

/** an action that commits after the fuse unless the user undoes it */
export interface UndoableOpts {
  /** identifies the subject (e.g. a source_id); one fuse per key */
  key: string
  text: string
  commit: () => Promise<unknown>
  /** ok toast once the commit lands */
  doneText: string
  /** error toast text when the commit rejects */
  failText: (err: unknown) => string
}

interface ToastCtx {
  notify: (kind: ToastKind, text: string) => void
  /** start an undo fuse; the commit runs when it ends and lives here, not in the caller */
  notifyUndoable: (opts: UndoableOpts) => void
  /** keys whose fuse is running; callers hide these subjects */
  pending: string[]
  /** bumps each time a fused commit settles, ok or not; callers reload on change */
  settled: number
}

const NO_KEYS: string[] = []
const Ctx = createContext<ToastCtx>({ notify: () => {}, notifyUndoable: () => {}, pending: NO_KEYS, settled: 0 })

/** call from any component inside the shell to surface a transient toast */
export function useToast(): ToastCtx {
  return useContext(Ctx)
}

const TTL: Record<ToastKind, number> = { ok: 4000, info: 4000, err: 7000 }
const FUSE_MS = 8000

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const idRef = useRef(0)
  const timers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map())

  const dismiss = useCallback((id: number) => {
    setToasts((t) => t.filter((x) => x.id !== id))
    const tm = timers.current.get(id)
    if (tm) {
      clearTimeout(tm)
      timers.current.delete(id)
    }
  }, [])

  const notify = useCallback(
    (kind: ToastKind, text: string) => {
      const id = (idRef.current += 1)
      setToasts((t) => [...t, { id, kind, text }])
      timers.current.set(
        id,
        setTimeout(() => dismiss(id), TTL[kind]),
      )
    },
    [dismiss],
  )

  const [pending, setPending] = useState<string[]>(NO_KEYS)
  const [settled, setSettled] = useState(0)
  const fuses = useRef<Set<string>>(new Set())

  const release = useCallback(
    (key: string) => {
      fuses.current.delete(key)
      setPending((p) => p.filter((k) => k !== key))
    },
    [],
  )

  const notifyUndoable = useCallback(
    (o: UndoableOpts) => {
      if (fuses.current.has(o.key)) return
      fuses.current.add(o.key)
      setPending((p) => [...p, o.key])
      const id = (idRef.current += 1)
      setToasts((t) => [...t, { id, kind: 'info', text: o.text, fuse: o.key }])
      timers.current.set(
        id,
        setTimeout(() => {
          dismiss(id)
          o.commit()
            .then(
              () => notify('ok', o.doneText),
              (err: unknown) => notify('err', o.failText(err)),
            )
            .finally(() => {
              // the key stays pending while the commit is in flight so the card can't flash back
              release(o.key)
              setSettled((n) => n + 1)
            })
        }, FUSE_MS),
      )
    },
    [dismiss, notify, release],
  )

  const undo = useCallback(
    (t: Toast) => {
      release(t.fuse!)
      dismiss(t.id) // clears the timer, so nothing is sent
    },
    [dismiss, release],
  )

  // Unmounting the provider (the console itself closing) clears pending timers, which drops any
  // fused commit: like a reload or tab close, an action is only final once its bar has run out.
  useEffect(() => {
    const map = timers.current
    const open = fuses.current
    return () => {
      map.forEach(clearTimeout)
      map.clear()
      open.clear()
    }
  }, [])

  const value = useMemo(
    () => ({ notify, notifyUndoable, pending, settled }),
    [notify, notifyUndoable, pending, settled],
  )

  return (
    <Ctx.Provider value={value}>
      {children}
      <div className="toast-viewport" aria-live="polite" aria-atomic="false">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind}${t.fuse ? ' fused' : ''}`} role={t.kind === 'err' ? 'alert' : 'status'}>
            <span className="toast-ico">
              <Icon name={t.kind === 'err' ? 'alert' : t.kind === 'ok' ? 'check2' : 'info'} size={15} />
            </span>
            <span className="toast-text">{t.text}</span>
            {t.fuse ? (
              <>
                <button type="button" className="toast-undo" onClick={() => undo(t)}>
                  Undo
                </button>
                <span className="toast-fuse" aria-hidden="true" />
              </>
            ) : (
              <button className="toast-x" aria-label="Dismiss notification" onClick={() => dismiss(t.id)}>
                <Icon name="close" size={13} />
              </button>
            )}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  )
}
