// Code points that render as nothing or reorder what follows: control characters,
// bidi controls and marks, zero-width and other invisible format characters.
const HIDDEN_RANGES: [number, number][] = [
  [0x00, 0x08], [0x0b, 0x1f], [0x7f, 0x9f], [0xad, 0xad], [0x34f, 0x34f], [0x61c, 0x61c],
  [0x115f, 0x1160], [0x17b4, 0x17b5], [0x180b, 0x180f], [0x200b, 0x200f], [0x2028, 0x202e],
  [0x2060, 0x206f], [0x3164, 0x3164], [0xfe00, 0xfe0f], [0xfeff, 0xfeff], [0xffa0, 0xffa0],
  [0xfff0, 0xfffb], [0xe0000, 0xe0fff],
]

const hidden = (cp: number) => HIDDEN_RANGES.some(([lo, hi]) => cp >= lo && cp <= hi)

/** Text with each hidden code point shown as \u{XXXX}, so it reads as a tool receives it. */
export function visibleText(text: string): string {
  let out = ''
  for (const ch of text) {
    const cp = ch.codePointAt(0)!
    out += hidden(cp) ? `\\u{${cp.toString(16).toUpperCase().padStart(4, '0')}}` : ch
  }
  return out
}
