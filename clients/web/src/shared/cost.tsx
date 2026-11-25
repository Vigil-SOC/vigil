/* The one rule for rendering a spend amount and its provenance (#989).
   `zero` and `unknown` mean opposite things — a model that genuinely costs
   nothing versus one nobody could price — and neither is a dollar figure.
   A bare 0 with no provenance is a real zero: the backend stores an unpriced
   call as no cost at all and counts it separately (`unpriced_calls`, #1115). */

export type PricingSource = 'exact' | 'zero' | 'unknown'

const NOT_PRICED = 'not priced'
const NOT_BILLED = 'not billed'
const NOT_PRICED_HINT = 'No rate is known for this model, so its spend cannot be measured. Set one in the gateway’s pricing overrides; a hosted model that costs nothing gets its zero price there too. A provider whose model runs on hardware you control is stated self-hosted (config.self_hosted), which also prices it at zero.'
const NOT_BILLED_HINT = 'This model is not billed: its provider is stated self-hosted (config.self_hosted), meaning the model runs on hardware you control, or the gateway’s pricing overrides set its rate to zero.'

/** Provenance vocabulary, identical everywhere it appears. */
export const PROVENANCE_LABEL: Record<PricingSource, string> = {
  exact: 'exact',
  zero: NOT_BILLED,
  unknown: NOT_PRICED,
}

type CostView =
  | { kind: 'amount'; text: string; source?: PricingSource }
  | { kind: 'not-priced'; text: typeof NOT_PRICED; hint: string }
  | { kind: 'not-billed'; text: typeof NOT_BILLED; hint: string }

interface CostInput {
  usd: number | null | undefined
  source?: PricingSource | null
  digits?: number
}

const dollars = (n: number, digits: number) => `$${n.toFixed(digits)}`

export function describeCost({ usd, source, digits = 2 }: CostInput): CostView {
  if (source === 'unknown' || typeof usd !== 'number' || !Number.isFinite(usd)) {
    return { kind: 'not-priced', text: NOT_PRICED, hint: NOT_PRICED_HINT }
  }
  if (source === 'zero') return { kind: 'not-billed', text: NOT_BILLED, hint: NOT_BILLED_HINT }
  return { kind: 'amount', text: dollars(usd, digits), source: source ?? undefined }
}

/** " · $0.42 over" once spend passes its ceiling, else "". Strict on the unrounded figures; under a cent reads
 *  "under $0.01 over". A ceiling that is 0, null or not a number is no ceiling. */
export function fmtOver(spent: number | null | undefined, ceiling: number | null | undefined): string {
  if (typeof spent !== 'number' || typeof ceiling !== 'number' || !Number.isFinite(spent) || !Number.isFinite(ceiling)
    || ceiling <= 0 || !(spent > ceiling)) return ''
  const over = spent - ceiling
  return over < 0.01 ? ' · under $0.01 over' : ` · ${dollars(over, 2)} over`
}

/** Plain-text form for template strings and KPI values. */
export function fmtCost(usd: CostInput['usd'], source?: CostInput['source'], digits?: number): string {
  return describeCost({ usd, source, digits }).text
}

interface CostProps extends CostInput {
  className?: string
  /** Extra tooltip text; a not-priced / not-billed label prepends its own hint. */
  title?: string
}

/** Renders the rule as an element. Provenance is conveyed by text (and a title
 *  carrying the route to resolving it), never by colour alone. */
export function Cost({ className, title, ...input }: CostProps) {
  const v = describeCost(input)
  switch (v.kind) {
    case 'amount':
      return <span className={className} title={title}>{v.text}</span>
    case 'not-priced':
    case 'not-billed':
      return <span className={className} title={title ? `${v.hint} ${title}` : v.hint} data-cost={v.kind}>{v.text}</span>
    default: {
      const never: never = v
      return never
    }
  }
}
