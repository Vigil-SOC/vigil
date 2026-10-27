import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import ToolPicker from './ToolPicker'
import type { CatalogTool, ToolCatalog } from './toolCatalog'

const tool = (name: string, server: string, group: string, description: string): CatalogTool => ({ name, server, group, description, changes: 'read_only' })
const TOOLS = [
  tool('search_findings', 'vigil', 'Findings', 'Search findings by entity, source or text'),
  tool('list_cases', 'vigil', 'Cases', 'List cases, filter by entity'),
  tool('recall_entity', 'vigil', 'Memory', 'What Vigil remembers about a host or user'),
  tool('crowdstrike_falcon_contain_host', 'crowdstrike', 'CrowdStrike', 'Network-contain a host'),
]
const catalog: ToolCatalog = { phase: 'ready', tools: TOOLS, byName: new Map(TOOLS.map((t) => [t.name, t])) }

function pick(q: string) {
  const onChange = vi.fn()
  const { unmount } = render(<ToolPicker value={[]} onChange={onChange} catalog={catalog} />)
  const box = screen.getByRole('textbox')
  fireEvent.change(box, { target: { value: q } })
  fireEvent.keyDown(box, { key: 'Enter' })
  unmount()
  return onChange.mock.calls[0]?.[0]
}

describe('ToolPicker search', () => {
  it('Enter adds the tool whose name says the word, not one that only mentions it', () => {
    expect(pick('entity')).toEqual(['recall_entity'])
  })

  it('a word start in the name beats the description', () => {
    expect(pick('contain')).toEqual(['crowdstrike_falcon_contain_host'])
    expect(pick('find')).toEqual(['search_findings'])
  })
})
