/** `text` on one line, cut on a whole word with an ellipsis past `limit` characters; the full text belongs on hover. */
export function briefText(text: string, limit = 60): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (flat.length <= limit) return flat
  const head = flat.slice(0, limit + 1)
  const cut = head.includes(' ') ? head.slice(0, head.lastIndexOf(' ')) : flat.slice(0, limit)
  return `${cut.replace(/[\s.,;:—-]+$/, '')}…`
}
