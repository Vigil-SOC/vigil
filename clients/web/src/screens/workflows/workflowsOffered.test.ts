import { describe, it, expect } from 'vitest'
import { workflowsOffered } from './useWorkflowsData'

describe('workflowsOffered', () => {
  it('lists Ask Vigil first, then workflows with a read_skill agent; unknown agent ids are skipped', () => {
    const agents = [
      { id: 'hunter', recommended_tools: ['read_skill'] },
      { id: 'plain', recommended_tools: ['search'] },
    ]
    const workflows = [
      { name: 'Hunt', agents: ['plain', 'hunter'] },
      { name: 'Plain', agents: ['plain'] },
      { name: 'Ghost', agents: ['missing'] },
    ]
    expect(workflowsOffered(workflows, agents)).toEqual(['Ask Vigil', 'Hunt'])
    expect(workflowsOffered([], [])).toEqual(['Ask Vigil'])
  })
})
