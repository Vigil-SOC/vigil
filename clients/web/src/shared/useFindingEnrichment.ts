// The on-demand AI analysis of a finding, shared by FindingPopup and FindingDetails.
import { useCallback, useEffect, useRef, useState } from 'react'
import { findingsApi } from '../services/api'

export interface RelatedTechnique {
  technique_id: string
  technique_name: string
  relevance?: string
}

export interface Enrichment {
  threat_summary?: string
  threat_type?: string
  risk_level?: string
  confidence_score?: number
  potential_impact?: string
  recommended_actions?: string[]
  investigation_questions?: string[]
  related_techniques?: RelatedTechnique[]
  timeline_context?: string
  business_context?: string
  indicators?: { malicious_ips?: string[]; suspicious_domains?: string[]; [kind: string]: string[] | undefined }
  analysis_notes?: string
  raw_response?: string
  generated_at?: string
}

export type EnrichPhase = 'idle' | 'loading' | 'ready' | 'error'
export type EnrichError = 'not_configured' | 'budget' | 'forbidden' | 'failed'

// The daemon writes triage keys into ai_enrichment too; only a generated analysis carries generated_at.
export const isGeneratedAnalysis = (e: Enrichment | null | undefined): e is Enrichment => !!e?.generated_at

const errorKind = (e: unknown): EnrichError => {
  const response = (e as { response?: { status?: number; data?: { code?: string } } })?.response
  if (response?.status === 402 || response?.data?.code === 'BUDGET_EXCEEDED') return 'budget'
  if (response?.status === 503) return 'not_configured'
  if (response?.status === 403) return 'forbidden'
  return 'failed'
}

export const useFindingEnrichment = (findingId: string | null) => {
  const [enrichment, setEnrichment] = useState<Enrichment | null>(null)
  const [phase, setPhase] = useState<EnrichPhase>('idle')
  const [error, setError] = useState<EnrichError | null>(null)
  // An analysis can take minutes; a reply for a finding no longer on screen is dropped.
  const shown = useRef(findingId)

  useEffect(() => {
    shown.current = findingId
    setEnrichment(null)
    setPhase('idle')
    setError(null)
  }, [findingId])

  const seed = useCallback((stored: Enrichment | null | undefined) => {
    if (!isGeneratedAnalysis(stored)) return
    setEnrichment(stored)
    setPhase('ready')
  }, [])

  const load = (force = false) => {
    if (!findingId) return
    const target = findingId
    setPhase('loading')
    setError(null)
    findingsApi
      .getEnrichment(target, force)
      .then((res) => {
        if (shown.current !== target) return
        setEnrichment((res.data?.enrichment || null) as Enrichment | null)
        setPhase('ready')
      })
      .catch((e) => {
        if (shown.current !== target) return
        setError(errorKind(e))
        setPhase('error')
      })
  }

  return { enrichment, phase, error, seed, load }
}
