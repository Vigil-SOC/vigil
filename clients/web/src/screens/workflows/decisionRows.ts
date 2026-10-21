/* The v3 Decisions tab's row model: one row per turn, its outcome read from what the
   turn changed, identical consecutive turns collapsed, turns grouped in rounds. Kept
   out of WorkflowsScreen.tsx so it can be tested without rendering the screen. */

export interface V3Query { ref: string; purpose: string; query: string; rows: number | null; error: string | null }
export interface V3Created { id: string; kind: string; label: string }
export interface V3Changed { id: string; status: string; confidence: number; from: number; fromStatus: string }
export interface V3Move {
  n: number
  move: string
  targetId: string
  /** The target's label; empty when the run did not record one. */
  target: string
  /** Only a choose turn's reason is shown: it is the refusal, not the model's reasoning. */
  reason: string
  queries: V3Query[]
  created: V3Created[]
  changed: V3Changed[]
  prediction: { id: string; status: string } | null
  theories: number | null
  concluded: boolean
}

export interface DecisionRow {
  /** The turns this row stands for: more than one when identical consecutive turns collapsed. */
  turns: V3Move[]
  move: string
  targetId: string
  target: string
  outcome: string
  /** The turn did not move: the loop refused its choice or none was named. */
  refused: boolean
  searches: number
}
export interface DecisionRound { label: string; rows: DecisionRow[] }

const NOUN: Record<string, [string, string]> = {
  cause: ['cause', 'causes'], event: ['event', 'events'], actor: ['actor', 'actors'], boundary: ['boundary', 'boundaries'], link: ['link', 'links'],
}
const SHOWN = 3
const list = (ids: string[]) => (ids.length > SHOWN ? `${ids.slice(0, SHOWN).join(' ')} …` : ids.join(' '))

/** What the turn did to the graph, in a line. */
export function outcomeOf(m: V3Move): string {
  if (m.move === 'choose') return m.reason || 'no next move named'
  const parts: string[] = []
  const kinds = [...new Set(m.created.map((c) => c.kind))]
  for (const kind of kinds) {
    const made = m.created.filter((c) => c.kind === kind)
    const [one, many] = NOUN[kind] ?? [kind, `${kind}s`]
    parts.push(kind === 'link' ? `+${made.length} ${made.length === 1 ? one : many}` : `+${made.length} ${made.length === 1 ? one : many} ${list(made.map((c) => c.id))}`)
  }
  // A status change always reads; a confidence move only when it is more than rounding.
  const moved = m.changed.filter((c) => c.status !== c.fromStatus || Math.abs(c.confidence - c.from) >= 0.05)
  for (const c of moved.slice(0, SHOWN)) parts.push(c.status !== c.fromStatus ? `${c.id} ${c.status} ${c.confidence.toFixed(2)}` : `${c.id} ${c.from.toFixed(2)}→${c.confidence.toFixed(2)}`)
  if (moved.length > SHOWN) parts.push(`+${moved.length - SHOWN} more changed`)
  if (m.prediction) parts.push(`prediction ${m.prediction.id} ${m.prediction.status === 'absent' ? 'not found' : m.prediction.status}`)
  if (m.theories !== null) parts.push(m.theories === 0 ? 'no theories' : `${m.theories} ${m.theories === 1 ? 'theory' : 'theories'}`)
  if (m.concluded) parts.push('conclusion written')
  return parts.join(' · ') || 'nothing new'
}

/** Rounds start at each theorize. Moves before the first one are the opening round. */
export function decisionRounds(moves: readonly V3Move[]): { label: string; moves: V3Move[] }[] {
  const sorted = [...moves].sort((a, b) => a.n - b.n)
  const rounds: { label: string; moves: V3Move[] }[] = []
  let bucket: V3Move[] = []
  let round = 0
  const flush = (label: string) => {
    if (bucket.length === 0) return
    rounds.push({ label, moves: bucket })
    bucket = []
  }
  for (const move of sorted) {
    if (move.move === 'theorize') {
      flush(round === 0 ? 'Opening' : `Round ${round}`)
      round += 1
      bucket = [move]
    } else {
      bucket.push(move)
    }
  }
  flush(round === 0 ? 'Opening' : `Round ${round}`)
  return rounds
}

/** The rounds as rows. Consecutive turns with the same move, target and outcome are one row. */
export function decisionRows(moves: readonly V3Move[]): DecisionRound[] {
  return decisionRounds(moves).map((round) => {
    const rows: DecisionRow[] = []
    for (const m of round.moves) {
      const outcome = outcomeOf(m)
      const last = rows[rows.length - 1]
      if (last && last.move === m.move && last.targetId === m.targetId && last.outcome === outcome) {
        last.turns.push(m)
        last.searches += m.queries.length
      } else {
        rows.push({ turns: [m], move: m.move, targetId: m.targetId, target: m.target, outcome, refused: m.move === 'choose', searches: m.queries.length })
      }
    }
    return { label: round.label, rows }
  })
}
