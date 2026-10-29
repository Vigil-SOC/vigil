const BAND_MAX = 150 // the most the outcome bands may take, stacked
const OUT_PAD = 20 // space above the first outcome row and below the last
const OUT_SLOT = 44 // the least height an outcome row takes, so labels never crowd
const OUT_GAP = 8 // space between a band's edge and the next row

/**
 * Where each outcome lands and how thick its band is. Thickness is linear in the count; each row
 * takes at least OUT_SLOT and at least its band plus a gap, so neither bands nor labels overlap.
 * The scale shrinks until the rows fit the height.
 */
export function layoutOutcomes(counts: number[], H: number) {
  const avail = H - 2 * OUT_PAD
  const total = counts.reduce((n, c) => n + c, 0) || 1
  const slotMin = Math.min(OUT_SLOT, avail / Math.max(1, counts.length))
  const thick = (k: number) => counts.map((c) => (c === 0 ? 0 : Math.max(2.5, c * k)))
  const need = (k: number) => thick(k).reduce((n, t) => n + Math.max(slotMin, t + 2 * OUT_GAP), 0)
  let k = BAND_MAX / total
  if (need(k) > avail) {
    let lo = 0
    let hi = k
    for (let i = 0; i < 30; i++) {
      const mid = (lo + hi) / 2
      if (need(mid) > avail) hi = mid
      else lo = mid
    }
    k = lo
  }
  const th = thick(k)
  // Spare height is shared out between the rows, so they still spread over the board as before.
  const rowNeed = th.map((t) => Math.max(slotMin, t + 2 * OUT_GAP))
  const spare = Math.max(0, avail - rowNeed.reduce((n, x) => n + x, 0)) / Math.max(1, counts.length)
  const slots = rowNeed.map((h) => h + spare)
  let at = OUT_PAD
  const ys = slots.map((h) => {
    const y = at + h / 2
    at += h
    return y
  })
  return { th, ys }
}
