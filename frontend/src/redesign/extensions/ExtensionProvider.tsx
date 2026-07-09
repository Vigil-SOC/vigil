/* ============================================================
   ExtensionProvider — the runtime page-extension registry.

   On mount it reads the enabled integrations; any integration whose
   stored config carries a `connectorUrl` is treated as extension-capable
   (Vigil stays ignorant of LogLM specifically). For each, it fetches and
   validates the connector's manifest and exposes the resulting mount
   points. The registry is empty by default — an OSS Vigil with no
   connectors configured shows no extension tabs.
   ============================================================ */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { configApi } from '../../services/api'
import { fetchManifest } from './registry'
import type { ExtensionMountPoint, RegisteredExtension } from './contracts'

export interface ResolvedMountPoint {
  ext: RegisteredExtension
  mount: ExtensionMountPoint
}

interface ExtensionsValue {
  extensions: RegisteredExtension[]
  /** flattened (extension, screen-mount-point) pairs, ready for the shell */
  mountPoints: ResolvedMountPoint[]
  loading: boolean
}

const Ctx = createContext<ExtensionsValue>({
  extensions: [],
  mountPoints: [],
  loading: true,
})

export function useExtensions(): ExtensionsValue {
  return useContext(Ctx)
}

interface IntegrationsResponse {
  enabled_integrations?: string[]
  integrations?: Record<string, { connectorUrl?: string } | undefined>
}

export function ExtensionProvider({ children }: { children: ReactNode }) {
  const [extensions, setExtensions] = useState<RegisteredExtension[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const ctrl = new AbortController()
    ;(async () => {
      setLoading(true)
      try {
        const res = await configApi.getIntegrations()
        const data = (res.data as IntegrationsResponse) || {}
        const enabled = data.enabled_integrations || []
        const configs = data.integrations || {}
        const candidates = enabled
          .map((id) => ({ id, url: configs[id]?.connectorUrl }))
          .filter((c): c is { id: string; url: string } => !!c.url)
        const results = await Promise.all(
          candidates.map((c) => fetchManifest(c.id, c.url, ctrl.signal)),
        )
        if (ctrl.signal.aborted) return
        setExtensions(results.filter((r): r is RegisteredExtension => r !== null))
      } catch {
        if (!ctrl.signal.aborted) setExtensions([])
      } finally {
        if (!ctrl.signal.aborted) setLoading(false)
      }
    })()
    return () => ctrl.abort()
  }, [])

  const mountPoints = useMemo<ResolvedMountPoint[]>(
    () =>
      extensions.flatMap((ext) =>
        ext.manifest.mountPoints
          .filter((mount) => mount.type === 'screen')
          .map((mount) => ({ ext, mount })),
      ),
    [extensions],
  )

  const value = useMemo<ExtensionsValue>(
    () => ({ extensions, mountPoints, loading }),
    [extensions, mountPoints, loading],
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
