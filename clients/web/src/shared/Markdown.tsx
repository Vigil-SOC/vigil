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

function citeLink(id: string): MdNode {
  return { type: 'link', url: `${CITE_HREF}${encodeURIComponent(id)}`, children: [{ type: 'text', value: id }] }
}

/** `text` with each match of `pattern` turned into a cite link. */
function split(text: string, pattern: RegExp): MdNode[] {
  const out: MdNode[] = []
  let at = 0
  for (const match of text.matchAll(pattern)) {
    const start = match.index ?? 0
    if (start > at) out.push({ type: 'text', value: text.slice(at, start) })
    out.push(citeLink(match[0]))
    at = start + match[0].length
  }
  if (at === 0) return [{ type: 'text', value: text }]
  if (at < text.length) out.push({ type: 'text', value: text.slice(at) })
  return out
}

/** A remark pass: ids in prose, or alone in an inline code span, become cite links. Code blocks and links keep their text. */
function remarkCite(pattern: RegExp) {
  const whole = new RegExp(`^(?:${pattern.source})$`)
  const walk = (node: MdNode) => {
    if (!node.children || node.type === 'link' || node.type === 'linkReference') return
    node.children = node.children.flatMap((child) => {
      if (child.type === 'text' && child.value) return split(child.value, pattern)
      if (child.type === 'inlineCode' && child.value && whole.test(child.value.trim())) return [citeLink(child.value.trim())]
      walk(child)
      return [child]
    })
  }
  return () => (tree: MdNode) => walk(tree)
}

export interface MarkdownCite {
  /** Matches the ids to draw as chips (see shell/citations.ts, citePattern). */
  pattern: RegExp
  render: (id: string) => ReactNode
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
              <>{cite.render(decodeURIComponent(href.slice(CITE_HREF.length)))}</>
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
