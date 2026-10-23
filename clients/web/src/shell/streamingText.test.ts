import { describe, expect, it } from 'vitest'
import { showableTail, splitStreaming } from './streamingText'

describe('an answer still being written', () => {
  it('settles blocks at a blank line and leaves the rest open', () => {
    expect(splitStreaming('First para.\n\nSecond **bo')).toEqual({ blocks: [{ text: 'First para.\n', joined: false }], tail: 'Second **bo', joined: false })
  })

  it('does not settle a blank line inside a code fence', () => {
    const text = 'Run:\n\n```\na\n\nb'
    expect(splitStreaming(text)).toEqual({ blocks: [{ text: 'Run:\n', joined: false }], tail: '```\na\n\nb', joined: false })
  })

  it('settles each finished item of a long list, so the open end stays short', () => {
    expect(splitStreaming('Findings:\n\n1. one\n2. two\n3. thr')).toEqual({
      blocks: [
        { text: 'Findings:\n', joined: false },
        { text: '1. one\n', joined: false },
        { text: '2. two\n', joined: true },
      ],
      tail: '3. thr',
      joined: true,
    })
  })

  it('never draws an unclosed mark raw', () => {
    expect(showableTail('this is **impor')).toBe('this is impor')
    expect(showableTail('run `whoami')).toBe('run whoami')
    expect(showableTail('see [the ale')).toBe('see the ale')
    expect(showableTail('see [the alert](https://exa')).toBe('see the alert')
    expect(showableTail('**done** and `ok` and [x](y)')).toBe('**done** and `ok` and [x](y)')
    // a backslash is literal inside code: the span still closes
    expect(showableTail('Path `C:\\Temp\\` then **bo')).toBe('Path `C:\\Temp\\` then bo')
  })

  it('shows a table only once its header rule has arrived, and a row only once it is complete', () => {
    expect(showableTail('Hosts:\n| host | hits')).toBe('Hosts:')
    expect(showableTail('Hosts:\n| host | hits |\n')).toBe('Hosts:')
    expect(showableTail('Hosts:\n| host | hits |\n|---|---')).toBe('Hosts:')
    expect(showableTail('| host | hits |\n|---|---|\n| 10.0.0.4 | 3')).toBe('| host | hits |\n|---|---|')
    expect(showableTail('| host | hits |\n|---|---|\n| 10.0.0.4 | 3 |\n')).toBe('| host | hits |\n|---|---|\n| 10.0.0.4 | 3 |\n')
  })

  it('keeps a fence nested under a list item open across its blank lines', () => {
    expect(splitStreaming('1. step\n\n    ```\n    a\n\n    b').tail).toBe('    ```\n    a\n\n    b')
  })

  it('opens a code fence as soon as it starts, marks and all', () => {
    expect(showableTail('```spl\nindex=main **')).toBe('```spl\nindex=main **')
  })
})
