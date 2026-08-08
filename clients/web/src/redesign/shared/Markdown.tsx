/* Shared Markdown renderer — used by the chat drawer and case details.
   Styling lives under the reusable `.md` class in styles.css. */
import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'

const MarkdownBase = ({ children }: { children: string }) => (
  <div className="md">
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkBreaks]}
      components={{
        a: ({ href, children }) => (
          <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
        ),
      }}
    >
      {children}
    </ReactMarkdown>
  </div>
)

export const Markdown = memo(MarkdownBase)
