import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import { Box, alpha, useTheme } from '@mui/material'

interface MarkdownMessageProps {
  children: string
}

/**
 * Renders Markdown (GFM: headings, bold/italic, lists, links, code blocks,
 * tables) using MUI-themed styling. react-markdown v9 does not render raw
 * HTML by default, so message content cannot inject markup.
 */
function MarkdownMessageBase({ children }: MarkdownMessageProps) {
  const theme = useTheme()
  const codeBg = alpha(theme.palette.text.primary, 0.06)

  return (
    <Box
      sx={{
        fontSize: '0.875rem',
        lineHeight: 1.6,
        color: 'text.primary',
        wordBreak: 'break-word',
        '& > :first-of-type': { mt: 0 },
        '& > :last-child': { mb: 0 },
        '& p': { my: 0.75 },
        '& h1, & h2, & h3, & h4, & h5, & h6': {
          mt: 1.5,
          mb: 0.75,
          fontWeight: 700,
          lineHeight: 1.3,
        },
        '& h1': { fontSize: '1.25rem' },
        '& h2': { fontSize: '1.15rem' },
        '& h3': { fontSize: '1.05rem' },
        '& h4, & h5, & h6': { fontSize: '1rem' },
        '& ul, & ol': { my: 0.75, pl: 3 },
        '& li': { mb: 0.25 },
        '& li > p': { my: 0 },
        '& a': { color: 'primary.main', textDecoration: 'underline' },
        '& strong': { fontWeight: 700 },
        '& em': { fontStyle: 'italic' },
        '& blockquote': {
          my: 1,
          ml: 0,
          pl: 1.5,
          borderLeft: 3,
          borderColor: 'divider',
          color: 'text.secondary',
        },
        '& hr': { my: 1.5, border: 0, borderTop: 1, borderColor: 'divider' },
        '& code': {
          fontFamily: 'monospace',
          fontSize: '0.85em',
          bgcolor: codeBg,
          px: 0.5,
          py: 0.25,
          borderRadius: 0.5,
        },
        '& pre': {
          my: 1,
          p: 1.5,
          bgcolor: codeBg,
          borderRadius: 1,
          overflowX: 'auto',
        },
        '& pre code': {
          bgcolor: 'transparent',
          p: 0,
          fontSize: '0.8rem',
          display: 'block',
          whiteSpace: 'pre',
        },
        '& table': {
          my: 1,
          borderCollapse: 'collapse',
          width: '100%',
          fontSize: '0.8rem',
          display: 'block',
          overflowX: 'auto',
        },
        '& th, & td': {
          border: 1,
          borderColor: 'divider',
          px: 1,
          py: 0.5,
          textAlign: 'left',
        },
        '& th': { fontWeight: 700, bgcolor: codeBg },
        '& img': { maxWidth: '100%', borderRadius: 1 },
      }}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </Box>
  )
}

export const MarkdownMessage = memo(MarkdownMessageBase)
