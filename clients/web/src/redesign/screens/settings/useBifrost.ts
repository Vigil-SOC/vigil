/* Hooks over the Bifrost config passthrough. Same shape as useSettings.ts —
   a phase machine plus imperative actions — so the AI Config panels read the
   same way whichever store is behind them. */
import { useCallback, useEffect, useState } from 'react'
import {
  bifrostApi,
  type BifrostKey,
  type BifrostKeyWrite,
  type BifrostModel,
  type BifrostModelParameters,
  type BifrostProvider,
  type BifrostVirtualKey,
  type BifrostVirtualKeyWrite,
} from '../../../services/bifrostApi'

type Phase = 'loading' | 'ready' | 'error'

const errText = (e: unknown, fallback: string): string => {
  const r = e as { response?: { data?: { detail?: string; error?: { message?: string } } } }
  // Bifrost's own errors pass through the proxy verbatim, so its shape
  // ({error:{message}}) shows up alongside FastAPI's ({detail}).
  return r?.response?.data?.error?.message || r?.response?.data?.detail || (e as Error)?.message || fallback
}

/** Providers with their keys, loaded together — a provider with no keys can't route. */
export function useBifrostProviders() {
  const [providers, setProviders] = useState<BifrostProvider[]>([])
  const [keys, setKeys] = useState<Record<string, BifrostKey[]>>({})
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    bifrostApi
      .listProviders()
      .then(async (res) => {
        const list = res.data.providers || []
        const entries = await Promise.all(
          list.map(async (p) => {
            try {
              const r = await bifrostApi.listKeys(p.name)
              return [p.name, r.data.keys || []] as const
            } catch {
              return [p.name, []] as const
            }
          }),
        )
        if (cancelled) return
        setProviders(list)
        setKeys(Object.fromEntries(entries))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(errText(e, 'Failed to load Bifrost providers'))
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const saveKey = useCallback(
    async (provider: string, keyId: string | null, data: BifrostKeyWrite) => {
      const res = keyId
        ? await bifrostApi.updateKey(provider, keyId, data)
        : await bifrostApi.createKey(provider, data)
      reload()
      return res.data
    },
    [reload],
  )

  const removeKey = useCallback(
    async (provider: string, keyId: string) => {
      await bifrostApi.removeKey(provider, keyId)
      reload()
    },
    [reload],
  )

  const addProvider = useCallback(
    async (name: string) => {
      const res = await bifrostApi.createProvider(name)
      reload()
      return res.data
    },
    [reload],
  )

  const removeProvider = useCallback(
    async (name: string) => {
      await bifrostApi.removeProvider(name)
      reload()
    },
    [reload],
  )

  return { providers, keys, phase, error, reload, saveKey, removeKey, addProvider, removeProvider }
}

/** The gateway's model catalog. `query` filters server-side. */
export function useBifrostModels(query: string) {
  const [models, setModels] = useState<BifrostModel[]>([])
  const [total, setTotal] = useState(0)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    const t = setTimeout(() => {
      bifrostApi
        .modelDetails(query || undefined)
        .then((res) => {
          if (cancelled) return
          setModels(res.data.models || [])
          setTotal(res.data.total || 0)
          setPhase('ready')
        })
        .catch((e) => {
          if (cancelled) return
          setError(errText(e, 'Failed to load model catalog'))
          setPhase('error')
        })
    }, query ? 250 : 0)
    return () => {
      cancelled = true
      clearTimeout(t)
    }
  }, [query])

  return { models, total, phase, error }
}

/** Pricing + capabilities for one model. Null until a model is selected. */
export function useModelParameters(model: string | null, provider: string | null) {
  const [params, setParams] = useState<BifrostModelParameters | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!model || !provider) {
      setParams(null)
      return
    }
    let cancelled = false
    setError(null)
    bifrostApi
      .modelParameters(model, provider)
      .then((res) => !cancelled && setParams(res.data))
      .catch((e) => {
        if (cancelled) return
        setParams(null)
        setError(errText(e, 'No pricing on record for this model'))
      })
    return () => {
      cancelled = true
    }
  }, [model, provider])

  return { params, error }
}

export function useVirtualKeys() {
  const [vks, setVks] = useState<BifrostVirtualKey[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    bifrostApi
      .listVirtualKeys()
      .then((res) => {
        if (cancelled) return
        setVks(res.data.virtual_keys || [])
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(errText(e, 'Failed to load virtual keys'))
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const save = useCallback(
    async (id: string | null, data: BifrostVirtualKeyWrite) => {
      const res = id
        ? await bifrostApi.updateVirtualKey(id, data)
        : await bifrostApi.createVirtualKey(data)
      reload()
      return res.data
    },
    [reload],
  )

  const remove = useCallback(
    async (id: string) => {
      await bifrostApi.removeVirtualKey(id)
      reload()
    },
    [reload],
  )

  return { vks, phase, error, reload, save, remove }
}

export { errText as bifrostError }

/** What a provider can route today. Empty list rather than an error when the
    gateway knows the provider but lists nothing for it. */
export function useProviderModels(provider: string) {
  const [models, setModels] = useState<string[]>([])
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    bifrostApi
      .providerModels(provider)
      .then((res) => {
        if (cancelled) return
        setModels((res.data.models || []).map((m) => m.name))
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setError(errText(e, 'Could not load this provider’s models'))
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [provider])

  return { models, phase, error }
}
