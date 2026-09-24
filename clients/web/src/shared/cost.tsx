/* The one rule for rendering a spend amount and its provenance (#989).
   `zero` and `unknown` mean opposite things — a model that genuinely costs
   nothing versus one nobody could price — and neither is a dollar figure.
   A bare 0 with no provenance is a real zero: the backend stores an unpriced
   call as no cost at all and counts it separately (`unpriced_calls`, #1115). */

export type PricingSource = 'exact' | 'heuristic' | 'zero' | 'unknown'

export const NOT_PRICED = 'not priced'
export const NOT_BILLED = 'not billed'
export const NOT_PRICED_HINT = 'No rate is known for this model, so its spend cannot be measured. Set one in the gateway’s pricing overrides.'
export const NOT_BILLED_HINT = 'This model is not billed. A rate for a self-hosted model is set in the gateway’s pricing overrides.'

/** Provenance vocabulary, identical everywhere it appears. */
export const PROVENANCE_LABEL: Record<PricingSource, string> = {
  exact: 'exact',
  heuristic: 'heuristic',
  zero: NOT_BILLED,
  unknown: NOT_PRICED,
}

export type CostView =
  | { kind: 'amount'; text: string; source?: PricingSource }
  | { kind: 'not-priced'; text: typeof NOT_PRICED; hint: string }
  | { kind: 'not-billed'; text: typeof NOT_BILLED; hint: string }

export interface CostInput {
  usd: number | null | undefined
  /** Upper bound of an estimate band; renders as `$low–$high`. */
  high?: number | null
  source?: PricingSource | null
  digits?: number
}

const dollars = (n: number, digits: number) => `$${n.toFixed(digits)}`

export function describeCost({ usd, high, source, digits = 2 }: CostInput): CostView {
  if (source === 'unknown' || typeof usd !== 'number' || !Number.isFinite(usd)) {
    return { kind: 'not-priced', text: NOT_PRICED, hint: NOT_PRICED_HINT }
  }
  if (source === 'zero') return { kind: 'not-billed', text: NOT_BILLED, hint: NOT_BILLED_HINT }
  const text = typeof high === 'number' && Number.isFinite(high) ? `${dollars(usd, digits)}–${dollars(high, digits)}` : dollars(usd, digits)
  return { kind: 'amount', text, source: source ?? undefined }
}

/** Plain-text form for template strings and KPI values. A heuristic estimate
 *  carries its provenance inline so the word travels with the number. */
export function fmtCost(usd: CostInput['usd'], source?: CostInput['source'], digits?: number): string {
  const v = describeCost({ usd, source, digits })
  return v.kind === 'amount' && v.source === 'heuristic' ? `${v.text} · ${PROVENANCE_LABEL.heuristic}` : v.text
}

interface CostProps extends CostInput {
  /** Prefix for an estimate, e.g. "~". Dropped when there is no figure to approximate. */
  approx?: boolean
  className?: string
  /** Extra tooltip text; a not-priced / not-billed label prepends its own hint. */
  title?: string
  /** Class for the inline provenance word on a heuristic amount. */
  sourceClassName?: string
}

/** Renders the rule as an element. Provenance is conveyed by text (and a title
 *  carrying the route to resolving it), never by colour alone. */
export function Cost({ approx, className, sourceClassName, title, ...input }: CostProps) {
  const v = describeCost(input)
  switch (v.kind) {
    case 'amount':
      return (
        <span className={className} title={title}>
          {approx ? '~' : ''}{v.text}
          {v.source === 'heuristic' && <span className={sourceClassName}> · {PROVENANCE_LABEL.heuristic}</span>}
        </span>
      )
    case 'not-priced':
    case 'not-billed':
      return <span className={className} title={title ? `${v.hint} ${title}` : v.hint} data-cost={v.kind}>{v.text}</span>
    default: {
      const never: never = v
      return never
    }
  }
}
