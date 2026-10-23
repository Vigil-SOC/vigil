/* An answer still being written, split so only its open end re-renders (#3037). */

const FENCE = /^\s*(```|~~~)/
const LIST_ITEM = /^(?:[-*+]|\d{1,9}[.)])[ \t]/
const TABLE_ROW = /^\s*\|/
const TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/

/** A settled block. `joined` continues the list the block before it ends with. */
export interface Block {
  text: string
  joined: boolean
}

/**
 * Settled blocks end at a blank line outside a code fence, or where a list's next top-level item
 * starts: they never change again, so each is parsed once. The tail is everything after the last
 * one, re-rendered as text arrives.
 */
export function splitStreaming(text: string): { blocks: Block[]; tail: string; joined: boolean } {
  const blocks: Block[] = []
  let fenced: string | null = null
  let start = 0
  let at = 0
  let joined = false
  for (const line of text.split('\n')) {
    const end = at + line.length
    const open = text.slice(start, at)
    const fence = FENCE.exec(line)?.[1]
    if (fence !== undefined && (fenced === null || fenced === fence)) fenced = fenced === null ? fence : null
    else if (fenced === null && end < text.length && line.trim() === '' && open.trim() !== '') {
      blocks.push({ text: open, joined })
      start = end + 1
      joined = false
    } else if (fenced === null && LIST_ITEM.test(line) && LIST_ITEM.test(open)) {
      // a long list is one block between blank lines; each finished item settles on its own
      blocks.push({ text: open, joined })
      start = at
      joined = true
    }
    at = end + 1
  }
  return { blocks, tail: text.slice(start), joined }
}

/** The tail as it can be shown: no unclosed mark drawn raw, no half table. An open fence shows as its box. */
export function showableTail(tail: string): string {
  const lines = tail.split('\n')
  if (openFence(lines)) return tail
  return closeInline(withoutHalfTable(lines).join('\n'))
}

/** Whether the lines end inside a code fence; a `~~~` inside a ``` block does not close it. */
function openFence(lines: string[]): boolean {
  let fenced: string | null = null
  for (const line of lines) {
    const fence = FENCE.exec(line)?.[1]
    if (fence !== undefined && (fenced === null || fenced === fence)) fenced = fenced === null ? fence : null
  }
  return fenced !== null
}

/** A table row appears once its line is complete, and a table once its header rule has arrived. */
function withoutHalfTable(lines: string[]): string[] {
  // the line still being written, or the row just finished when nothing follows it yet
  const open = lines.length > 1 && lines[lines.length - 1] === '' ? lines.length - 2 : lines.length - 1
  if (!TABLE_ROW.test(lines[open])) return lines
  let first = open
  while (first > 0 && TABLE_ROW.test(lines[first - 1])) first -= 1
  const ready = open - first >= 2 && TABLE_RULE.test(lines[first + 1])
  if (!ready) return lines.slice(0, first)
  return open === lines.length - 1 ? lines.slice(0, open) : lines
}

/** Drops an unclosed `**`, backtick run or `[` (with any half-written `](href`), so what follows reads as plain text. */
function closeInline(text: string): string {
  let code: [number, number] | null = null // an open backtick run: where, how long
  let bold = -1
  let link = -1
  let href = -1
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i]
    if (c === '\\' && code === null) {
      i += 1
      continue
    }
    if (href >= 0) {
      if (c === ')') link = href = -1
      continue
    }
    if (c === '`') {
      let n = 1
      while (text[i + n] === '`') n += 1
      if (code === null) code = [i, n]
      else if (code[1] === n) code = null
      i += n - 1
      continue
    }
    if (code !== null) continue
    if (c === '*' && text[i + 1] === '*') {
      bold = bold < 0 ? i : -1
      i += 1
    } else if (c === '[') link = i
    else if (c === ']' && link >= 0) {
      if (text[i + 1] === '(') href = i
      else link = -1
    }
  }
  const cuts: Array<[number, number]> = []
  if (code !== null) cuts.push([code[0], code[0] + code[1]])
  if (bold >= 0) cuts.push([bold, bold + 2])
  if (link >= 0) cuts.push([link, link + 1])
  if (href >= 0) cuts.push([href, text.length])
  let out = text
  for (const [from, to] of cuts.sort((a, b) => b[0] - a[0])) out = out.slice(0, from) + out.slice(to)
  return out
}
