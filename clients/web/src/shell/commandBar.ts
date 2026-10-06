/** Command palette rows. The last four are display-only. */

export type LiveCommandId = 'investigate' | 'hunt' | 'replay' | 'ask' | 'ticket'

export type CommandId = LiveCommandId | 'hold' | 'isolate' | 'phish' | 'custom'

export interface CommandDef {
  id: CommandId
  name: string
  hint: string
  /** One line for the Commands tab. */
  desc: string
  /** What it starts, as the bar's run() does; "—" for a Later row. */
  runs: string
  later: boolean
  /** The workflow a live command starts, when it starts one. */
  workflowId?: string
}

export const COMMANDS: CommandDef[] = [
  { id: 'investigate', name: '/investigate', hint: '<finding or context>', desc: 'Run incident response on a finding or a description of what you saw', runs: 'Incident response workflow', later: false, workflowId: 'incident-response' },
  { id: 'hunt', name: '/hunt', hint: '<hypothesis>', desc: 'Start a hypothesis-driven threat hunt', runs: 'Threat hunt workflow', later: false, workflowId: 'threat-hunt' },
  { id: 'replay', name: '/replay', hint: '<case>', desc: 'Open a case by id', runs: 'Opens the case', later: false },
  { id: 'ask', name: '/ask', hint: '<question>', desc: 'Ask Vigil your question in chat', runs: 'Opens Ask Vigil', later: false },
  { id: 'ticket', name: '/ticket', hint: '<case>', desc: 'Create a Jira ticket from a case', runs: 'Exports the case to Jira', later: false },
  { id: 'hold', name: '/hold', hint: '<case or kind>', desc: 'Hold a case, or every case of one kind', runs: '—', later: true },
  { id: 'isolate', name: '/isolate', hint: '<host>', desc: 'Isolate a host. Irreversible, always asks you to confirm', runs: '—', later: true },
  { id: 'phish', name: '/phish', hint: '<message or sender>', desc: 'Run Phishing triage on a reported email', runs: '—', later: true },
  { id: 'custom', name: 'Custom commands', hint: '', desc: 'Create a command for any workflow', runs: '—', later: true },
]

export const LIVE_COMMANDS = COMMANDS.filter((c) => !c.later)

export type DestTag = 'Case' | 'Finding' | 'Page' | 'Command' | 'Recent'

export interface BoardLink {
  key: string
  label: string
}

export interface Hit {
  id: string
  title: string
}

export interface SearchHits {
  caseExact: Hit | null
  findingExact: Hit | null
  textCases: Hit[]
  iocCases: Hit[]
}

export interface PaletteRow {
  key: string
  label: string
  hint: string
  dest: DestTag
  disabled: boolean
  commandId?: CommandId
  caseId?: string
  page?: string
  recent?: string
}

export interface JiraConfigRead {
  enabled_integrations?: string[]
  integrations?: Record<string, { url?: string; username?: string; project_key?: string } | undefined>
  secrets_set?: Record<string, Record<string, boolean> | undefined>
}

export interface JiraReadiness {
  gap: string | null
  projectKey: string
}

export function recentsStorageKey(userId: string): string {
  return `vigil.command.recents.${userId}`
}

export function readRecents(userId: string): string[] {
  try {
    const raw = localStorage.getItem(recentsStorageKey(userId))
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed.filter((item): item is string => typeof item === 'string' && item.trim() !== '')
  } catch {
    return []
  }
}

export function writeRecent(userId: string, text: string): string[] {
  const trimmed = text.trim()
  if (!trimmed) return readRecents(userId)
  const next = [trimmed, ...readRecents(userId).filter((item) => item !== trimmed)].slice(0, 8)
  localStorage.setItem(recentsStorageKey(userId), JSON.stringify(next))
  return next
}

export function isLiveCommand(id: CommandId): id is LiveCommandId {
  return id === 'investigate' || id === 'hunt' || id === 'replay' || id === 'ask' || id === 'ticket'
}

/** Text after the command name. Empty when the query is not that command. */
export function commandRemainder(query: string, name: string): string {
  const trimmed = query.trim()
  const lower = trimmed.toLowerCase()
  const command = name.toLowerCase()
  if (lower === command) return ''
  if (lower.startsWith(`${command} `)) return trimmed.slice(name.length).trim()
  return ''
}

export function jiraReadiness(data: JiraConfigRead | null): JiraReadiness {
  if (!data) return { gap: 'Jira configuration could not be read', projectKey: '' }
  const enabled = (data.enabled_integrations ?? []).includes('jira')
  const jira = data.integrations?.jira ?? {}
  const projectKey = (jira.project_key ?? '').trim()
  if (!enabled) return { gap: 'Jira is not enabled', projectKey }
  const missing: string[] = []
  if (!(jira.url ?? '').trim()) missing.push('url')
  if (!(jira.username ?? '').trim()) missing.push('username')
  if (!data.secrets_set?.jira?.api_token) missing.push('api_token')
  if (!projectKey) missing.push('project_key')
  if (missing.length > 0) return { gap: `Jira is missing ${missing.join(', ')}`, projectKey }
  return { gap: null, projectKey }
}

export function commandPreview(
  id: LiveCommandId,
  arg: string,
  jira: JiraReadiness,
): { line: string; disabled: boolean } {
  const a = arg.trim()
  switch (id) {
    case 'investigate':
      return a
        ? { line: `Run incident response for ${a}`, disabled: false }
        : { line: 'Add a finding id or context', disabled: true }
    case 'hunt':
      return a
        ? { line: `Start a threat hunt: ${a}`, disabled: false }
        : { line: 'Add a hypothesis', disabled: true }
    case 'replay':
      return a
        ? { line: `Open case ${a}`, disabled: false }
        : { line: 'Add a case id', disabled: true }
    case 'ask':
      return a
        ? { line: `Ask Vigil: ${a}`, disabled: false }
        : { line: 'Add a question', disabled: true }
    case 'ticket':
      if (!a) return { line: 'Add a case id', disabled: true }
      if (jira.gap) return { line: jira.gap, disabled: true }
      return { line: `Export case ${a} to Jira project ${jira.projectKey}`, disabled: false }
    default: {
      const exhaustive: never = id
      return exhaustive
    }
  }
}

function commandToken(query: string): string {
  return query.trim().toLowerCase().split(/\s+/)[0] ?? ''
}

function commandsFor(query: string): CommandDef[] {
  const q = query.trim().toLowerCase()
  if (!q) return LIVE_COMMANDS
  if (q.startsWith('/')) {
    const token = commandToken(query)
    return COMMANDS.filter((command) => {
      if (command.name.startsWith('/')) return command.name.toLowerCase().startsWith(token)
      return token === '/' || token === 'custom' || token.startsWith('custom')
    })
  }
  return COMMANDS.filter((command) => command.name.toLowerCase().includes(q))
}

function commandRow(command: CommandDef): PaletteRow {
  return {
    key: `cmd:${command.id}`,
    label: command.name,
    hint: command.hint,
    dest: 'Command',
    disabled: command.later,
    commandId: command.id,
  }
}

function pushCase(rows: PaletteRow[], seen: Set<string>, hit: Hit): void {
  if (!hit.id || seen.has(hit.id)) return
  seen.add(hit.id)
  rows.push({
    key: `case:${hit.id}`,
    label: hit.title || hit.id,
    hint: hit.id,
    dest: 'Case',
    disabled: false,
    caseId: hit.id,
  })
}

export function buildRows(
  query: string,
  hits: SearchHits | null,
  recents: string[],
  boards: BoardLink[],
): PaletteRow[] {
  const q = query.trim()
  const ql = q.toLowerCase()
  if (!q) {
    return [
      ...recents.map((text) => ({
        key: `recent:${text}`,
        label: text,
        hint: '',
        dest: 'Recent' as const,
        disabled: false,
        recent: text,
      })),
      ...LIVE_COMMANDS.map(commandRow),
    ]
  }

  if (q.startsWith('/')) return commandsFor(q).map(commandRow)

  const rows: PaletteRow[] = []
  const seen = new Set<string>()
  if (hits?.caseExact) pushCase(rows, seen, hits.caseExact)
  if (hits?.findingExact?.id) {
    const hit = hits.findingExact
    rows.push({
      key: `finding:${hit.id}`,
      label: hit.title || hit.id,
      hint: hit.id,
      dest: 'Finding',
      disabled: false,
    })
  }
  for (const hit of hits?.textCases ?? []) pushCase(rows, seen, hit)
  for (const hit of hits?.iocCases ?? []) pushCase(rows, seen, hit)
  for (const board of boards) {
    if (!board.label.toLowerCase().includes(ql)) continue
    rows.push({
      key: `page:${board.key}`,
      label: board.label,
      hint: '',
      dest: 'Page',
      disabled: false,
      page: board.key,
    })
  }
  for (const command of commandsFor(q)) rows.push(commandRow(command))
  for (const text of recents) {
    if (!text.toLowerCase().includes(ql)) continue
    rows.push({
      key: `recent:${text}`,
      label: text,
      hint: '',
      dest: 'Recent',
      disabled: false,
      recent: text,
    })
  }
  return rows
}

export function firstEnabled(rows: PaletteRow[]): number {
  return rows.findIndex((row) => !row.disabled)
}

export function moveEnabled(rows: PaletteRow[], from: number, direction: 1 | -1): number {
  if (rows.length === 0) return -1
  let index = from
  for (let step = 0; step < rows.length; step += 1) {
    index = (index + direction + rows.length) % rows.length
    if (!rows[index].disabled) return index
  }
  return from
}
