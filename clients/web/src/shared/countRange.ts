import { useSearchParams } from 'react-router-dom'
import type { CountRange } from '../services/api'

/** `?range=` on the current URL; Today, the default, leaves the param off. */
export function useCountRange(): [CountRange, (next: CountRange) => void] {
  const [params, setParams] = useSearchParams()
  const range: CountRange = params.get('range') === 'all' ? 'all' : 'today'
  const set = (next: CountRange) =>
    setParams(
      (prev) => {
        const out = new URLSearchParams(prev)
        if (next === 'all') out.set('range', 'all')
        else out.delete('range')
        return out
      },
      { replace: true },
    )
  return [range, set]
}

/** `href` carrying the range, so a linked screen counts over the same span. */
export function withRange(href: string, range: CountRange): string {
  if (range !== 'all') return href
  return `${href}${href.includes('?') ? '&' : '?'}range=all`
}
