import { memo, useMemo, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'

/** The few mdast fields the id pass reads; the full types are a transitive dependency. */
interface MdNode {
  type: string
  value?: string
  url?: string
  children?: MdNode[]
}

/** Where a recognised id goes: a fragment link, so the URL sanitiser keeps it, drawn by `a` below. */
const CITE_HREF = '#vg-cite:'
/** An id already drawn as its chip in this paragraph: named again, not chipped again. */
const AGAIN_HREF = '#vg-cite-again:'

function citeLink(id: string, again = false): MdNode {
  return { type: 'link', url: `${again ? AGAIN_HREF : CITE_HREF}${encodeURIComponent(id)}`, children: [{ type: 'text', value: id }] }
}

/** `text` with each match of `pattern` turned into a cite link; one already `seen` is marked a repeat. */
function split(text: string, pattern: RegExp, seen: Set<string>): MdNode[] {
  const out: MdNode[] = []
  let at = 0
  for (const match of text.matchAll(pattern)) {
    const again = seen.has(match[0])
    seen.add(match[0])
    const start = match.index ?? 0
    if (start > at) out.push({ type: 'text', value: text.slice(at, start) })
    out.push(citeLink(match[0], again))
    at = start + match[0].length
  }
  if (at === 0) return [{ type: 'text', value: text }]
  if (at < text.length) out.push({ type: 'text', value: text.slice(at) })
  return out
}

/** The id a node draws as a chip: a cite link, or one wrapped alone in bold or italics. */
function citedBy(node: MdNode): string | null {
  for (const href of [CITE_HREF, AGAIN_HREF]) {
    if (node.type === 'link' && node.url?.startsWith(href)) return decodeURIComponent(node.url.slice(href.length))
  }
  if ((node.type === 'strong' || node.type === 'emphasis') && node.children?.length === 1) return citedBy(node.children[0])
  return null
}

/**
 * A model writes an alert's raw source id and severity beside it: "dl-005 (crowdstrike-dl-005, critical)".
 * The chip names the alert, so the id goes: "(critical)" is kept, "(crowdstrike-dl-005)" is dropped (#2459).
 */
function withoutTrailer(text: string, id: string): string {
  const match = text.match(/^(\s*)\(([^()]*)\)/)
  if (!match) return text
  const parts = match[2].split(',').map((part) => part.trim())
  const kept = parts.filter((part) => part !== id && !part.endsWith(`-${id}`) && !part.endsWith(`_${id}`))
  if (kept.length === parts.length) return text
  const rest = text.slice(match[0].length)
  return kept.length > 0 ? `${match[1]}(${kept.join(', ')})${rest}` : rest
}

/** Blocks that are read as one paragraph: an id is drawn as its chip once in each (#2459). */
const PARAGRAPHS = new Set(['paragraph', 'heading', 'tableCell'])

/** A remark pass: ids in prose, or alone in an inline code span, become cite links. Code blocks and links keep their text. */
function remarkCite(pattern: RegExp) {
  const whole = new RegExp(`^(?:${pattern.source})$`)
  const walk = (node: MdNode, inherited: Set<string>) => {
    if (!node.children || node.type === 'link' || node.type === 'linkReference') return
    const seen = PARAGRAPHS.has(node.type) ? new Set<string>() : inherited
    const children = node.children.flatMap((child) => {
      if (child.type === 'text' && child.value) return split(child.value, pattern, seen)
      if (child.type === 'inlineCode' && child.value && whole.test(child.value.trim())) {
        const id = child.value.trim()
        const again = seen.has(id)
        seen.add(id)
        return [citeLink(id, again)]
      }
      walk(child, seen)
      return [child]
    })
    node.children = children.flatMap((child, i) => {
      const before = i > 0 ? citedBy(children[i - 1]) : null
      if (!before || child.type !== 'text' || !child.value) return [child]
      const value = withoutTrailer(child.value, before)
      return value ? [{ ...child, value }] : []
    })
  }
  return () => (tree: MdNode) => walk(tree, new Set())
}

export interface MarkdownCite {
  /** Matches the ids to draw as chips (see shell/citations.ts, citePattern). */
  pattern: RegExp
  /** `again` when the id was already drawn as its chip earlier in the same paragraph. */
  render: (id: string, again: boolean) => ReactNode
}

const MarkdownBase = ({ children, cite }: { children: string; cite?: MarkdownCite }) => {
  const pattern = cite?.pattern
  const remarkPlugins = useMemo(
    () => (pattern ? [remarkGfm, remarkBreaks, remarkCite(pattern)] : [remarkGfm, remarkBreaks]),
    [pattern],
  )
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        components={{
          a: ({ href, children }) =>
            cite && href?.startsWith(CITE_HREF) ? (
              <>{cite.render(decodeURIComponent(href.slice(CITE_HREF.length)), false)}</>
            ) : cite && href?.startsWith(AGAIN_HREF) ? (
              <>{cite.render(decodeURIComponent(href.slice(AGAIN_HREF.length)), true)}</>
            ) : (
              <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
            ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}

export const Markdown = memo(MarkdownBase)
