import { useEffect, useRef, useState } from 'react'
import './hold-approve.css'

/** Matches the `.home-hold.holding .fill` transition in hold-approve.css. */
const HOLD_MS = 1600

export function HoldApprove({ disabled, onConfirm }: { disabled: boolean; onConfirm: () => void }) {
  const [holding, setHolding] = useState(false)
  const timer = useRef<number | null>(null)
  const disabledRef = useRef(disabled)
  disabledRef.current = disabled

  const clear = () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current)
      timer.current = null
    }
    setHolding(false)
  }

  const start = () => {
    if (disabledRef.current || timer.current !== null) return
    setHolding(true)
    timer.current = window.setTimeout(() => {
      timer.current = null
      setHolding(false)
      if (disabledRef.current) return
      onConfirm()
    }, HOLD_MS)
  }

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
    },
    [],
  )

  return (
    <button
      type="button"
      className={holding ? 'btn danger home-hold holding' : 'btn danger home-hold'}
      disabled={disabled}
      aria-label="Approve. Press and hold to confirm; this cannot be undone."
      onPointerDown={(event) => {
        if (event.button != null && event.button !== 0) return
        event.currentTarget.setPointerCapture?.(event.pointerId)
        start()
      }}
      onPointerUp={clear}
      onPointerCancel={clear}
      onKeyDown={(event) => {
        if (event.repeat || (event.key !== ' ' && event.key !== 'Enter')) return
        event.preventDefault()
        start()
      }}
      onKeyUp={(event) => {
        if (event.key === ' ' || event.key === 'Enter') clear()
      }}
    >
      <span className="fill" aria-hidden="true" />
      <span className="label">{holding ? 'Keep holding…' : 'Approve'}</span>
    </button>
  )
}
