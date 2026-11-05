import { useCallback, useEffect, useState } from 'react'
import { detectionRulesApi } from '../../services/api'
import type { Phase } from '../cases/useCases'

/** pass / fail / unknown / unchecked counts for one check */
export type CheckTally = Record<string, number>

/** a tool's rule count, and for a SIEM one tally per check key */
export type ToolTally = { rules: number } & Record<string, CheckTally | number>

export interface CheckResult {
  result?: string
  reason?: string | null
  checked_at?: string
  data_source?: string | null
}

export interface RuleRow {
  rule_id: number
  id: string
  title: string
  tool: string
  version: number
  checks: Record<string, CheckResult>
}

export interface BlindSpot {
  data_source: string
  rules: { rule_id: number; id: string; title: string; tool: string; reason?: string | null; checked_at?: string }[]
}

export interface RulesHealth {
  by_tool: Record<string, ToolTally>
  unchecked: Record<string, { tools: string[]; reason: string }>
  not_read: string
  blind_spots: BlindSpot[]
  not_passing: {
    rules: RuleRow[]
    total: number
    limit: number
    /** per SIEM: rules failing or unknown on a check; broken (Runs fails) and starved (Sees fails, Runs doesn't) can't fire */
    by_tool: Record<string, { not_passing: number; broken: number; starved: number }>
  }
  repo: { rules: number; deployed: number; not_deployed: number; siems: number }
}

export const REPO_TOOLS = new Set(['repo', 'overlay'])

/** The check keys a SIEM tool tallies, in the order the route sent them. */
export function checkKeys(t: ToolTally): string[] {
  return Object.keys(t).filter((k) => k !== 'rules' && typeof t[k] === 'object')
}

/** What an unchecked result means for this check on this tool. */
export function uncheckedReason(h: RulesHealth, check: string, tool: string): string {
  const u = h.unchecked[check]
  return u && u.tools.includes(tool) ? u.reason : 'not checked for this tool yet'
}

export function useRules() {
  const [data, setData] = useState<RulesHealth | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    detectionRulesApi
      .getHealth()
      .then((res) => {
        if (cancelled) return
        setData(res.data as RulesHealth)
        setPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setData(null)
        setError((e as { message?: string })?.message || 'Failed to load rule health')
        setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  return { data, phase, error, reload }
}
