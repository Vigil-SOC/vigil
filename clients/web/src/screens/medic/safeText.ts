/* Every Medic string goes through safeText before it is shown (MEDIC.md §10). Medic
   stores what came from logs, packs and admins, so none of it is trusted: it renders as
   React text children only, never as HTML, Markdown or a link. */

// ANSI escapes: CSI (ESC [ … or the 8-bit 0x9b form) and OSC (ESC ] … up to BEL or ST), then any other two-byte ESC sequence
// eslint-disable-next-line no-control-regex
const ANSI = /\u001b\[[0-?]*[ -/]*[@-~]|\u009b[0-?]*[ -/]*[@-~]|(?:\u001b\]|\u009d)[^\u0007\u001b\u009c]*(?:\u0007|\u001b\\|\u009c)?|\u001b[@-Z\\-_]/g
// C0/C1 controls except \n and \t, DEL, bidi overrides/isolates/marks, zero-width joiners and the BOM
// eslint-disable-next-line no-control-regex, no-irregular-whitespace
const UNSAFE = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f؜​-‏‪-‮⁦-⁩﻿]/g

const escapeChar = (c: string) => {
  const n = c.charCodeAt(0)
  return n < 0x100 ? `\\x${n.toString(16).padStart(2, '0')}` : `\\u${n.toString(16).padStart(4, '0')}`
}

/** ANSI stripped, other control and bidi characters shown as visible `\x1b`-style escapes. */
export function safeText(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value).replace(ANSI, '').replace(UNSAFE, escapeChar)
}

/** For a `title` tooltip: the same text, cut short so a 10 KB string cannot become a 10 KB tooltip. */
export function safeTitle(value: unknown, max = 400): string {
  const t = safeText(value)
  return t.length > max ? `${t.slice(0, max)}…` : t
}
