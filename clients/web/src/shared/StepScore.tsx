/** A containment's step score for the person deciding it (#5278): band, lowest factor and its raise line, factors on expand. */

type Factor = { value?: number | null }
type Score = {
  value?: number | null
  band?: string
  lowest?: string | null
  raise?: string | null
  factors?: Record<string, Factor> | null
  factors_in_force?: string[] | null
}

const FACTOR_LABEL: Record<string, string> = { precedent: 'Precedent', blind_spots: 'Blind spots', run_quality: 'Run quality' }
const BAND_LABEL: Record<string, string> = { high: 'High', medium: 'Medium', low: 'Low' }

const label = (name: string) => FACTOR_LABEL[name] ?? name
const num = (v: number | null | undefined) => (typeof v === 'number' ? v.toFixed(2) : 'unknown')

/** The one-line read: a band with "none" and an unknown value said in words, never as 0. */
function stepScoreLine(score: Score): string {
  if (score.band === 'none') return 'No factors in force'
  const lowest = score.lowest ? label(score.lowest) : null
  if (typeof score.value !== 'number') return lowest ? `Unknown · ${lowest} unknown` : 'Unknown'
  const head = `${BAND_LABEL[score.band ?? ''] ?? score.band} ${score.value.toFixed(2)}`
  return lowest ? `${head} · lowest: ${lowest}` : head
}

/** Nothing for an approval with no score: one filed any other way, or before scores. */
export function StepScore({ score }: { score: unknown }) {
  if (!score || typeof score !== 'object') return null
  const s = score as Score
  const inForce = new Set(s.factors_in_force ?? [])
  const factors = Object.entries(s.factors ?? {})
  return (
    <div className="step-score">
      <span className="step-score-line">{stepScoreLine(s)}</span>
      {s.raise && <span className="step-score-raise">{s.raise}</span>}
      {factors.length > 0 && (
        <details className="step-score-factors">
          <summary>Factors</summary>
          <ul>
            {factors.map(([name, f]) => (
              <li key={name}>
                <span>{label(name)}</span>
                <span>{inForce.has(name) ? num(f?.value) : `${num(f?.value)} · off`}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
