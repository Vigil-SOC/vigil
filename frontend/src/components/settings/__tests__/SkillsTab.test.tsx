import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

// Mock the skills API before importing the component.
vi.mock('../../../services/skillsApi', async () => {
  const actual = await vi.importActual<typeof import('../../../services/skillsApi')>(
    '../../../services/skillsApi'
  )
  return {
    ...actual,
    skillsApi: {
      list: vi.fn(),
      get: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      remove: vi.fn(),
      generate: vi.fn(),
      importZip: vi.fn(),
    },
  }
})

import { skillsApi, Skill } from '../../../services/skillsApi'
import SkillsTab from '../SkillsTab'

const SAMPLE_SKILLS: Skill[] = [
  {
    skill_id: 's-20260421-AAAA0001',
    name: 'Detect Lateral RDP',
    description: 'Finds unusual RDP sessions',
    category: 'detection',
    input_schema: {},
    output_schema: {},
    required_tools: ['splunk.search'],
    prompt_template: 'Look for RDP',
    execution_steps: [],
    is_active: true,
    version: 1,
  },
  {
    skill_id: 's-20260421-BBBB0002',
    name: 'Full IOC Enrichment',
    description: 'VT + Shodan + MISP',
    category: 'enrichment',
    input_schema: {},
    output_schema: {},
    required_tools: ['virustotal.hash', 'shodan.ip'],
    prompt_template: 'Enrich IOC',
    execution_steps: [],
    is_active: true,
    version: 1,
  },
]

describe('SkillsTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders skill cards with name, category, and required tools', async () => {
    (skillsApi.list as any).mockResolvedValue(SAMPLE_SKILLS)

    render(<SkillsTab />)

    expect(await screen.findByText('Detect Lateral RDP')).toBeInTheDocument()
    expect(await screen.findByText('Full IOC Enrichment')).toBeInTheDocument()

    // Category chips (text appears because category is rendered as Chip label)
    expect(screen.getByText('detection')).toBeInTheDocument()
    expect(screen.getByText('enrichment')).toBeInTheDocument()

    // Required tools chips
    expect(screen.getByText('splunk.search')).toBeInTheDocument()
    expect(screen.getByText('virustotal.hash')).toBeInTheDocument()
  })

  it('shows empty state when there are no skills', async () => {
    (skillsApi.list as any).mockResolvedValue([])

    render(<SkillsTab />)

    expect(await screen.findByText(/No skills yet/i)).toBeInTheDocument()
    // Both the header button and the empty-state button are called build-skill-ish.
    expect(screen.getAllByRole('button', { name: /Build/i }).length).toBeGreaterThan(0)
  })

  it('opens the Skill Builder dialog when Build Skill is clicked', async () => {
    (skillsApi.list as any).mockResolvedValue([])

    render(<SkillsTab />)

    // Wait for the empty state to render.
    await screen.findByText(/No skills yet/i)

    const buildButton = screen.getAllByRole('button', { name: /Build Skill/i })[0]
    fireEvent.click(buildButton)

    await waitFor(() => {
      expect(screen.getByText(/Build a Skill/i)).toBeInTheDocument()
      expect(screen.getByLabelText(/Capability description/i)).toBeInTheDocument()
    })
  })

  it('toggles active state via the switch', async () => {
    (skillsApi.list as any).mockResolvedValue([SAMPLE_SKILLS[0]])
    ;(skillsApi.update as any).mockResolvedValue({ ...SAMPLE_SKILLS[0], is_active: false })

    render(<SkillsTab />)

    await screen.findByText('Detect Lateral RDP')

    const toggle = screen.getByRole('checkbox')
    fireEvent.click(toggle)

    await waitFor(() => {
      expect(skillsApi.update).toHaveBeenCalledWith(
        's-20260421-AAAA0001',
        { is_active: false }
      )
    })
  })
})
