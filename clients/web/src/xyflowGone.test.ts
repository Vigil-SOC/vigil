/* The graph builder is gone: nothing in the console may import its canvas library. */
import { describe, expect, it } from 'vitest'

describe('no graph canvas', () => {
  it('has no source file that mentions @xyflow', () => {
    const files = import.meta.glob('./**/*.{ts,tsx,css}', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
    const hits = Object.entries(files).filter(([path, text]) => !path.endsWith('xyflowGone.test.ts') && text.includes('@xyflow')).map(([path]) => path)
    expect(Object.keys(files).length).toBeGreaterThan(50)
    expect(hits).toEqual([])
  })
})
