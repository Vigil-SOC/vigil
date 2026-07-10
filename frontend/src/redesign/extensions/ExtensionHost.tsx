/* ============================================================
   ExtensionHost — the generic screen that delivers a page extension.

   On mount it: dynamically imports the connector's ES-module bundle
   (which defines the custom element), instantiates the element, seeds it
   with host-context (theme tokens + a freshly-minted session token +
   apiBase), and appends it. It relays the element's events onto Vigil's
   own affordances (toast, router, full-view) and keeps the element's
   theme/token in sync without remounting.

   A load failure degrades to an inline "page unavailable" panel — the
   rest of the console keeps working (the shell also wraps screens in an
   ErrorBoundary as a backstop).
   ============================================================ */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { extensionsApi } from '../../services/api'
import { Icon } from '../shared/icons'
import { useToast, type ToastKind } from '../shell/toast'
import { useSocTheme } from '../shell/theme'
import type { ScreenProps } from '../shared/types'
import {
  EXTENSION_EVENT,
  type ExtensionEvent,
  type HostContext,
  type HostContextElement,
  type RegisteredExtension,
  type ExtensionMountPoint,
} from './contracts'

interface Props extends ScreenProps {
  ext: RegisteredExtension
  mount: ExtensionMountPoint
}

type Status = 'loading' | 'ready' | 'error'

/** how long to wait for the bundle to define its custom element before
 *  giving up (a bundle that loads but never defines the tag) */
const DEFINE_TIMEOUT_MS = 10_000

const SEVERITY_TO_TOAST: Record<string, ToastKind> = {
  info: 'info',
  warn: 'err',
  error: 'err',
  success: 'ok',
}

export default function ExtensionHost({ ext, mount, setViewFull }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const elRef = useRef<HostContextElement | null>(null)
  const ctxRef = useRef<HostContext | null>(null)
  const { mode, accent } = useSocTheme()
  const { notify } = useToast()
  const navigate = useNavigate()
  const [status, setStatus] = useState<Status>('loading')
  const [errorMsg, setErrorMsg] = useState('')

  const { bundleUrl, elementTag } = ext.manifest.render

  // mint a fresh session token + snapshot the current theme into a context
  const buildContext = useCallback(async (): Promise<HostContext> => {
    const res = await extensionsApi.getSessionToken(ext.integrationId)
    const { token, user } = res.data
    return {
      themeTokens: { '--accent': accent.a, mode },
      // token is null when no mint secret is configured — mount session-less
      // rather than failing (the connector is then expected to be open).
      session: token ? { token, user } : undefined,
      apiBase: ext.connectorUrl,
    }
  }, [ext.integrationId, ext.connectorUrl, accent.a, mode])

  // push the current context ref onto the element (no remount)
  const applyContext = useCallback(() => {
    if (elRef.current && ctxRef.current) elRef.current.hostContext = { ...ctxRef.current }
  }, [])

  // ---- load bundle + mount element (keyed on the bundle/tag only) --------
  useEffect(() => {
    let cancelled = false
    const container = containerRef.current
    if (!container) return
    setStatus('loading')
    setErrorMsg('')
    ;(async () => {
      try {
        // Defense in depth: the registry already origin-locks bundleUrl to the
        // connector, but this is the line that runs remote code in our origin.
        if (new URL(bundleUrl).origin !== new URL(ext.connectorUrl).origin) {
          throw new Error('bundle origin does not match connector origin')
        }
        await import(/* @vite-ignore */ bundleUrl)
        // wait for the element to register (resolves immediately on revisit)
        await Promise.race([
          customElements.whenDefined(elementTag),
          new Promise((_, reject) =>
            setTimeout(
              () => reject(new Error(`element "${elementTag}" not defined`)),
              DEFINE_TIMEOUT_MS,
            ),
          ),
        ])
        if (cancelled) return
        const el = document.createElement(elementTag) as HostContextElement
        el.style.display = 'block'
        el.style.height = '100%'
        elRef.current = el
        // seed context before append so the element's first render has it
        ctxRef.current = await buildContext()
        if (cancelled) return
        el.hostContext = { ...ctxRef.current }
        container.appendChild(el)
        // `ready` will flip us to 'ready'; if the element never emits it,
        // treat a successful mount as ready so we don't spin forever.
        setStatus((s) => (s === 'error' ? s : 'ready'))
      } catch (e) {
        if (!cancelled) {
          setStatus('error')
          setErrorMsg((e as Error)?.message || String(e))
          console.error(`[extensions] failed to load "${ext.integrationId}":`, e)
        }
      }
    })()
    return () => {
      cancelled = true
      const el = elRef.current
      if (el?.parentNode) el.parentNode.removeChild(el)
      elRef.current = null
      ctxRef.current = null
    }
    // buildContext intentionally excluded — theme changes are handled by the
    // sync effect below, not by remounting the element.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bundleUrl, elementTag, ext.integrationId])

  // ---- keep theme tokens in sync (no remount) ---------------------------
  useEffect(() => {
    if (!ctxRef.current) return
    ctxRef.current = { ...ctxRef.current, themeTokens: { '--accent': accent.a, mode } }
    applyContext()
  }, [accent.a, mode, applyContext])

  // ---- relay element events onto Vigil affordances ----------------------
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const handler = (evt: Event) => {
      const detail = (evt as CustomEvent).detail as ExtensionEvent | undefined
      if (!detail || typeof detail.type !== 'string') return
      switch (detail.type) {
        case 'ready':
          setStatus((s) => (s === 'error' ? s : 'ready'))
          break
        case 'navigate': {
          const to = detail.payload?.to
          // only follow in-app routes; ignore anything else (open-redirect guard)
          if (typeof to === 'string' && to.startsWith('/')) navigate(to)
          break
        }
        case 'notify':
          notify(
            SEVERITY_TO_TOAST[detail.payload?.severity] ?? 'info',
            String(detail.payload?.message ?? ''),
          )
          break
        case 'setViewFull':
          setViewFull(Boolean(detail.payload?.full))
          break
        case 'requestContextRefresh':
          buildContext()
            .then((ctx) => {
              ctxRef.current = ctx
              applyContext()
            })
            .catch((e) => console.warn('[extensions] context refresh failed:', e))
          break
        case 'error':
          setStatus('error')
          setErrorMsg(String(detail.payload?.message ?? 'Extension reported an error'))
          break
        default:
          break // unknown event types ignored → forward-compatible
      }
    }
    container.addEventListener(EXTENSION_EVENT, handler as EventListener)
    return () => container.removeEventListener(EXTENSION_EVENT, handler as EventListener)
  }, [navigate, notify, setViewFull, buildContext, applyContext])

  return (
    <div className="extension-host" style={{ height: '100%', position: 'relative' }}>
      {status === 'loading' && (
        <div className="extension-host-status">
          <Icon name="refresh" size={22} />
          <p>Loading {mount.navLabel}…</p>
        </div>
      )}
      {status === 'error' && (
        <div className="access-denied">
          <Icon name="alert" size={26} />
          <h2>{mount.navLabel} is unavailable</h2>
          <p>
            Couldn’t load this extension from {ext.connectorUrl}. The connector may be
            offline or misconfigured. The rest of Vigil is unaffected.
          </p>
          {errorMsg && <p style={{ opacity: 0.6, fontSize: 12 }}>{errorMsg}</p>}
        </div>
      )}
      <div
        ref={containerRef}
        style={{ height: '100%', display: status === 'ready' ? 'block' : 'none' }}
      />
    </div>
  )
}
