import { describe, expect, it } from 'vitest'
import { getGraphEndpointId } from './entityGraphUtils'

describe('getGraphEndpointId', () => {
  it('keeps string ids unchanged', () => {
    expect(getGraphEndpointId('ip-10.0.0.1')).toBe('ip-10.0.0.1')
  })

  it('extracts ids from force-graph-mutated node endpoints', () => {
    expect(getGraphEndpointId({ id: 'host-web-01', x: 12, y: 34 })).toBe(
      'host-web-01'
    )
  })
})
