import { useCallback, useEffect, useRef, useState } from 'react'
import { federationApi } from '../../services/api'
import { Select, TextInput, Toggle } from '../../shared/ui'
import { useFederation } from '../settings/useSettings'

const SEVERITY_OPTIONS = [
  { value: '', label: 'Any' },
  { value: 'low', label: 'Low+' },
  { value: 'medium', label: 'Medium+' },
  { value: 'high', label: 'High+' },
  { value: 'critical', label: 'Critical only' },
]

// poll-now only sets a flag the collector reads on its next cycle, so Test waits
// for last_poll_at to move, then gives up rather than loop forever
export const TEST_POLL_MS = 3_000
export const TEST_POLL_TRIES = 10

const QUEUED = 'Queued · the collector runs it on its next cycle'

/** Collection settings and a Test button for one federation source. */
export default function SourceCollection({ sourceId }: { sourceId: string }) {
  const { sources, globalEnabled, phase, error, reload, setGlobal, patchSource, pollNow } =
    useFederation()
  // typed text, so the hook's 10s refresh can't overwrite it and a blank field isn't saved as 0
  const [interval, setIntervalDraft] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<{ ok: boolean | null; text: string } | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout>>()
  const alive = useRef(true)
  // reset on mount too: StrictMode runs mount, cleanup, mount
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      clearTimeout(timer.current)
    }
  }, [])

  const source = sources.find((s) => s.source_id === sourceId)

  const guard = useCallback(async (run: () => Promise<unknown>, message: string) => {
    setFailure(null)
    try {
      await run()
    } catch {
      if (alive.current) setFailure(message)
    }
  }, [])

  const runTest = async () => {
    if (!source) return
    setResult(null)
    setTesting(true)
    let before = source.last_poll_at
    try {
      // the hook's copy can be 10s old, and a regular cycle in that gap would pass for the test
      const fresh = await federationApi.listSources().catch(() => null)
      before = fresh?.data.sources?.find((s) => s.source_id === sourceId)?.last_poll_at ?? before
      const queued = await pollNow(sourceId)
      if (!queued.data.ok) throw new Error('not queued')
    } catch {
      setTesting(false)
      setResult({ ok: false, text: 'Could not queue the test' })
      return
    }
    const finish = (r: { ok: boolean | null; text: string }) => {
      if (!alive.current) return
      setTesting(false)
      setResult(r)
    }
    const check = async (left: number) => {
      if (!alive.current) return
      try {
        const res = await federationApi.listSources()
        const now = res.data.sources?.find((s) => s.source_id === sourceId)
        if (now && now.last_poll_at !== before) {
          return finish(
            now.last_error
              ? { ok: false, text: now.last_error }
              : {
                  ok: true,
                  text: now.last_success_at
                    ? `Last success ${new Date(now.last_success_at).toLocaleString()}`
                    : 'Polled',
                },
          )
        }
      } catch {
        /* try again until the wait runs out */
      }
      if (left <= 1) return finish({ ok: null, text: QUEUED })
      timer.current = setTimeout(() => check(left - 1), TEST_POLL_MS)
    }
    timer.current = setTimeout(() => check(TEST_POLL_TRIES), TEST_POLL_MS)
  }

  if (phase === 'loading') {
    return <p className="text-xs text-tx-3">Loading collection settings…</p>
  }
  if (phase === 'error') {
    return (
      <p className="text-xs text-tx-3">
        Couldn&apos;t load collection settings: {error}{' '}
        <button className="text-accent-2 hover:underline" onClick={reload}>
          Retry
        </button>
      </p>
    )
  }
  if (!source) {
    return (
      <p className="text-xs text-tx-3">
        Alert collection isn&apos;t available for this source yet. The collector adds it when it
        next starts.
      </p>
    )
  }

  const canTest = source.enabled && globalEnabled
  const hint = !source.enabled
    ? 'Turn on Collect alerts to test.'
    : !globalEnabled
      ? 'Turn on alert collection above to test.'
      : null

  return (
    <div className="flex flex-col gap-3 border-t border-line-soft pt-3">
      {!globalEnabled && (
        <div className="flex items-center justify-between gap-3 text-xs text-tx-2">
          <span>Alert collection is off for this install, so no source polls.</span>
          <span className="flex items-center gap-2 shrink-0">
            <span className="text-tx-3">Turn on</span>
            <Toggle
              checked={false}
              label="Alert collection"
              onChange={(v) => guard(() => setGlobal(v), 'Could not change alert collection')}
            />
          </span>
        </div>
      )}
      <div className="flex items-center justify-between gap-3">
        <span className="flex flex-col">
          <span className="text-[13px] font-semibold text-tx">Collect alerts</span>
          <span className="text-xs text-tx-3">
            {source.is_configured ? 'Pull alerts from this source on a schedule.' : 'Not configured yet.'}
          </span>
        </span>
        <Toggle
          checked={source.enabled}
          label="Collect alerts"
          disabled={!source.is_configured}
          onChange={(v) => guard(() => patchSource(sourceId, { enabled: v }), 'Could not save')}
        />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1 text-xs font-semibold text-tx-2">
          Interval (s)
          <TextInput
            type="number"
            min={10}
            max={86400}
            value={interval ?? source.interval_seconds}
            onChange={(e) => setIntervalDraft(e.target.value)}
            onBlur={() => {
              const next = Number(interval)
              setIntervalDraft(null)
              if (interval === null || !Number.isInteger(next) || next < 10 || next > 86400) return
              if (next !== source.interval_seconds)
                guard(() => patchSource(sourceId, { interval_seconds: next }), 'Could not save')
            }}
          />
        </label>
        <div className="flex flex-col gap-1 text-xs font-semibold text-tx-2">
          Minimum severity
          <Select
            value={source.min_severity || ''}
            options={SEVERITY_OPTIONS}
            onSelect={(v) =>
              guard(() => patchSource(sourceId, { min_severity: v }), 'Could not save')
            }
          />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <button className="btn ghost" disabled={!canTest || testing} onClick={runTest}>
          {testing ? 'Testing…' : 'Test'}
        </button>
        <span className="text-xs min-w-0" role="status">
          {hint && !testing && !result && <span className="text-tx-3">{hint}</span>}
          {testing && <span className="text-tx-3">Waiting for the collector…</span>}
          {result && (
            <span className={result.ok === false ? 'text-high' : result.ok ? 'text-tx-2' : 'text-tx-3'}>
              {result.text}
            </span>
          )}
        </span>
      </div>
      {failure && (
        <p className="text-xs text-high" role="alert">
          {failure}
        </p>
      )}
    </div>
  )
}
