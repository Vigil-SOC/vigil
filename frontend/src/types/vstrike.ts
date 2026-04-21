/**
 * Frontend types for VStrike enrichment.
 *
 * Mirrors `backend/schemas/vstrike.py::VStrikeEnrichment`. The Pydantic
 * schema is the source of truth — keep these in sync when it changes.
 *
 * This data lives at `finding.entity_context.vstrike` and is produced by
 * CloudCurrent's VStrike fusion layer.
 */

export type VStrikeCriticality = 'low' | 'medium' | 'high' | 'critical'

export interface VStrikeAdjacentAsset {
  asset_id: string
  asset_name?: string
  segment?: string
  hop_distance: number
  /** MITRE ATT&CK technique ID for attack-path edges (e.g. "T1021.002"). */
  edge_technique?: string
}

export interface VStrikeEnrichment {
  asset_id: string
  asset_name?: string
  segment: string
  site?: string
  criticality: VStrikeCriticality
  mission_system?: string
  adjacent_assets: VStrikeAdjacentAsset[]
  /** Ordered asset_ids from initial access to the finding's asset. */
  attack_path: string[]
  blast_radius?: number
  topology_metadata?: Record<string, any>
  enriched_at: string
}

/**
 * Custom event name used to pivot the EntityGraph from the
 * NetworkContextPanel. Event `detail` carries `{ nodeId }`.
 */
export const VSTRIKE_GRAPH_HIGHLIGHT_EVENT = 'vstrike-graph-highlight'

export interface VStrikeGraphHighlightDetail {
  nodeId: string
}
