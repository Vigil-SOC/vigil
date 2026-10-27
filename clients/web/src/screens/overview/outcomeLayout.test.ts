import { describe, expect, it } from 'vitest'
import { layoutOutcomes } from './outcomeLayout'

// Each band, centred on its row, must stay inside the space between its neighbours' rows.
function overlaps(th: number[], ys: number[]) {
  for (let i = 1; i < ys.length; i++) {
    const prevBottom = ys[i - 1] + Math.max(th[i - 1], 10) / 2
    const top = ys[i] - Math.max(th[i], 10) / 2
    if (top < prevBottom) return true
    if (ys[i] - ys[i - 1] < 38) return true // the labels are 38px tall
  }
  return false
}

describe('layoutOutcomes', () => {
  it('keeps a dominant band from covering its neighbours', () => {
    const counts = [11, 15, 2, 1, 923, 0, 3]
    const { th, ys } = layoutOutcomes(counts, 400)
    expect(overlaps(th, ys)).toBe(false)
    expect(ys[0]).toBeGreaterThanOrEqual(20)
    expect(ys[ys.length - 1]).toBeLessThanOrEqual(380)
    expect(th[5]).toBe(0)
    expect(th[3]).toBeGreaterThanOrEqual(2.5) // one alert still shows
    expect(th[4]).toBeGreaterThan(th[1] * 10) // and the thickness stays proportional
  })

  it('fits evenly split outcomes and the taller full-screen board', () => {
    for (const H of [400, 600]) {
      const { th, ys } = layoutOutcomes([100, 100, 100, 100, 100, 100, 100], H)
      expect(overlaps(th, ys)).toBe(false)
      expect(ys[ys.length - 1]).toBeLessThanOrEqual(H - 20)
    }
  })

  it('handles a board with nothing in it', () => {
    const { th, ys } = layoutOutcomes([0, 0, 0, 0, 0, 0, 0], 400)
    expect(th.every((t) => t === 0)).toBe(true)
    expect(overlaps(th, ys)).toBe(false)
  })
})
