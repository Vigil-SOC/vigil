/**
 * The command bar's commands, one record each: what it takes, what its preview says,
 * and what it does. A line goes query -> parseLine -> lookup -> bindLine -> keyAction,
 * all derived in render, so nothing here can drift from what is typed.
 */

import { useEffect, useState } from 'react'
import { casesApi, configApi, findingsApi, searchApi, workflowApi } from '../services/api'
import type { Schema } from '../services/apiTypes'
import type { IconName } from '../shared/icons'
import { errorText } from '../shared/errorText'
import type { ConsoleScreenGoOptions } from '../shared/types'
import type { ToastKind } from './toast'

type RefKind = 'case' | 'alert'

/** What a command takes. A ref only ever binds to an exact id, so a search word cannot reach an API. */
type Args =
  | { kind: 'free'; rows?: RefKind }
  | { kind: 'ref'; ref: RefKind }

/** What a line resolved to. Only bindLine makes one, and run takes nothing else. */
export interface Bound {
  id: string
  text: string
}

/** What binding a line reads, beyond the line itself. */
export interface BindEnv {
  openCaseId: string | null
  jira: JiraReadiness
  attachment: HuntAttachment | null
}

/** What a run may reach in the console around the bar. */
export interface RunContext extends BindEnv {
  notify: (kind: ToastKind, text: string) => void
  onOpenCase: (caseId: string) => void
  onGo: (screen: string, options?: ConsoleScreenGoOptions) => void
  ask: (text?: string) => boolean
  /** Removes the attachment, unless another was attached while the run started. */
  dropSent: () => void
}

interface CommandText {
  id: string
  name: string
  hint: string
  /** One line for the Commands tab. */
  desc: string
  /** What it starts; "—" for a Later row. */
  runs: string
}

export interface LiveCommand extends CommandText {
  later: false
  /** It reads an attached file; the paperclip is off for the rest. */
  takesFile?: boolean
  /** The workflow it starts, when it starts one. */
  workflowId?: string
  args: Args
  /** Why a resolved line cannot run yet. */
  blocked?: (target: Bound, env: BindEnv) => string | null
  preview: (target: Bound, env: BindEnv) => { line: string; note?: string }
  run: (target: Bound, ctx: RunContext) => Promise<void>
}

interface LaterCommand extends CommandText {
  later: true
}

type CommandDef = LiveCommand | LaterCommand

const readyFile = (attachment: HuntAttachment | null) => (attachment?.status === 'ready' ? attachment : null)

/** A finding id when the text is one, otherwise what the person saw. */
async function findingOrContext(text: string): Promise<{ finding_id: string } | { context: string }> {
  try {
    await findingsApi.getById(encodeURIComponent(text))
    return { finding_id: text }
  } catch {
    return { context: text }
  }
}

/** The original stays on the case. The run has started, so a failed upload only warns. */
async function keepDocument(caseId: string, document: Extract<HuntAttachment, { status: 'ready' }>, started: string, ctx: RunContext) {
  await casesApi
    .attachDocument(caseId, document.file, { name: document.pasted ? PASTED_NAME : undefined, pages: document.pages })
    .catch((error: unknown) => ctx.notify('err', `${started}, but ${document.name} could not be kept on the case: ${errorText(error, 'upload failed')}`))
}

export const COMMANDS: CommandDef[] = [
  {
    id: 'investigate',
    name: '/investigate',
    hint: '<finding or context>',
    desc: 'Run incident response on a finding or a description of what you saw',
    runs: 'Incident response workflow',
    later: false,
    takesFile: true,
    workflowId: 'incident-response',
    args: { kind: 'free', rows: 'alert' },
    blocked: ({ text }) => (text ? null : 'Add a finding id or context'),
    preview: ({ text }, { attachment }) => ({ line: `Run incident response for ${text}${attachment?.status === 'ready' ? ` with ${attachment.name}` : ''}` }),
    run: async ({ text }, ctx) => {
      const document = readyFile(ctx.attachment)
      // The server opens the case, or reuses the open one holding the alert.
      const started = await workflowApi.execute('incident-response', {
        ...(await findingOrContext(text)),
        ...(document && { document: document.text }),
      })
      const caseId = started.data.case_id
      if (!caseId) throw new Error('The run started without a case')
      if (document) await keepDocument(caseId, document, 'The run started', ctx)
      ctx.onOpenCase(caseId)
      ctx.dropSent()
      ctx.notify('ok', 'Started Incident response workflow')
    },
  },
  {
    id: 'hunt',
    name: '/hunt',
    hint: '<hypothesis>',
    desc: 'Start a hypothesis-driven threat hunt',
    runs: 'Threat hunt workflow',
    later: false,
    takesFile: true,
    workflowId: 'threat-hunt',
    args: { kind: 'free' },
    blocked: ({ text }, { attachment }) => {
      if (huntHypothesis(text, attachment)) return null
      if (attachment?.status !== 'ready') return 'Type a hypothesis, or attach a file to have one proposed'
      return attachment.proposal?.status === 'none' ? attachment.proposal.reason : 'Checking what the document covers…'
    },
    preview: ({ text }, { attachment }) => {
      const running = runningHunt(attachment)
      return {
        line: `Start a threat hunt: ${huntHypothesis(text, attachment)}`,
        ...(!text && { note: running ? huntStateNote(running) : 'Proposed from the document. Type your own to replace it.' }),
      }
    },
    run: async ({ text }, ctx) => {
      // What was typed, else what the attached document proposed.
      const hypothesis = huntHypothesis(text, ctx.attachment)
      const document = readyFile(ctx.attachment)
      // The server puts the run on a case, so the drawer has something to open on.
      const started = await workflowApi.execute('threat-hunt', {
        hypothesis,
        ...(document && { document: document.text }),
        // The proposal's own subjects and approval ride with it, unedited.
        ...(!text && document?.proposal?.status === 'proposed' && {
          hypothesis_subjects: document.proposal.subjects,
          approve_hypotheses: document.proposal.approve,
        }),
      })
      const caseId = started.data.case_id
      if (!caseId) throw new Error('The hunt started without a case')
      // After the run is queued, so a refused hunt leaves no original behind.
      if (document) await keepDocument(caseId, document, 'The hunt started', ctx)
      ctx.onOpenCase(caseId)
      ctx.dropSent()
      ctx.notify('ok', `Hunt started on case "${huntTitle(hypothesis)}"`)
    },
  },
  {
    id: 'replay',
    name: '/replay',
    hint: '<case>',
    desc: "Watch a case's latest run",
    runs: 'Opens Watch a run',
    later: false,
    args: { kind: 'ref', ref: 'case' },
    preview: ({ id }) => ({ line: `Watch the latest run on case ${id}` }),
    run: async ({ id }, ctx) => {
      // Investigations come newest first.
      const runId = (await casesApi.getById(id)).data.investigations?.[0]?.run_id
      if (runId) ctx.onGo('workflows', { search: `?run=${encodeURIComponent(runId)}` })
      else {
        ctx.onOpenCase(id)
        ctx.notify('info', `Case ${id} has no run to replay`)
      }
    },
  },
  {
    id: 'ask',
    name: '/ask',
    hint: '<question>',
    desc: 'Ask Vigil your question in chat',
    runs: 'Opens Ask Vigil',
    later: false,
    takesFile: true,
    args: { kind: 'free' },
    blocked: ({ text }) => (text ? null : 'Add a question'),
    preview: ({ text }, { attachment }) => ({ line: `Ask Vigil: ${text}${attachment?.status === 'ready' ? ` (with ${attachment.name})` : ''}` }),
    run: async ({ text }, ctx) => {
      ctx.ask(text)
    },
  },
  {
    id: 'ticket',
    name: '/ticket',
    hint: '<case>',
    desc: 'Create a Jira ticket from a case',
    runs: 'Exports the case to Jira',
    later: false,
    args: { kind: 'ref', ref: 'case' },
    blocked: (_target, { jira }) => jira.gap,
    preview: ({ id }, { jira }) => ({ line: `Export case ${id} to Jira project ${jira.projectKey}` }),
    run: async ({ id }, ctx) => {
      ctx.notify('ok', await casesApi.exportJira(id, ctx.jira.projectKey))
    },
  },
  { id: 'hold', name: '/hold', hint: '<case or kind>', desc: 'Hold a case, or every case of one kind', runs: '—', later: true },
  { id: 'isolate', name: '/isolate', hint: '<host>', desc: 'Isolate a host. Irreversible, always asks you to confirm', runs: '—', later: true },
  { id: 'phish', name: '/phish', hint: '<message or sender>', desc: 'Run Phishing triage on a reported email', runs: '—', later: true },
  { id: 'custom', name: 'Custom commands', hint: '', desc: 'Create a command for any workflow', runs: '—', later: true },
]

export const LIVE_COMMANDS = COMMANDS.filter((c): c is LiveCommand => !c.later)

type DestTag = 'Case' | 'Alert' | 'Page' | 'Command' | 'Recent' | 'Ask' | 'Argument'

export type Section =
  | 'Recent searches'
  | 'Commands'
  | 'Custom commands'
  | 'Cases'
  | 'Alerts'
  | 'Pages'
  | 'Agents & workflows'
  | 'Ask Vigil'

export interface BoardLink {
  key: string
  label: string
  icon: IconName
  desc: string
}

/** What GET /search answers; a kind the role may not read comes back empty. */
export type SearchHits = Schema<'SearchResults'>
export type Hit = Schema<'SearchHit'>
type MatchType = NonNullable<Hit['match_type']>

export interface PaletteRow {
  key: string
  label: string
  hint: string
  dest: DestTag
  section: Section
  icon: IconName
  desc: string
  disabled: boolean
  commandId?: string
  caseId?: string
  findingId?: string
  page?: string
  /** The query string a Page row opens its page with. */
  search?: string
  recent?: string
  /** The line this row puts in the bar. */
  fill?: string
  /** This row names the command already typed, so there is nothing to move to. */
  echo?: boolean
}

interface JiraConfigRead {
  enabled_integrations?: string[]
  integrations?: Record<string, { url?: string; username?: string; project_key?: string } | undefined>
  secrets_set?: Record<string, Record<string, boolean> | undefined>
}

export interface JiraReadiness {
  gap: string | null
  projectKey: string
}

function recentsStorageKey(userId: string): string {
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

export function removeRecent(userId: string, text: string): string[] {
  const next = readRecents(userId).filter((item) => item !== text)
  localStorage.setItem(recentsStorageKey(userId), JSON.stringify(next))
  return next
}

export function writeRecent(userId: string, text: string): string[] {
  const trimmed = text.trim()
  if (!trimmed) return readRecents(userId)
  const next = [trimmed, ...readRecents(userId).filter((item) => item !== trimmed)].slice(0, 8)
  localStorage.setItem(recentsStorageKey(userId), JSON.stringify(next))
  return next
}

const HUNT_TITLE_MAX = 60

/** What the server reads (core/documents/extract.py); the picker offers these. */
export const ATTACH_TYPES = ['.pdf', '.docx', '.pptx', '.xlsx', '.png', '.jpg', '.jpeg', '.md', '.markdown', '.txt', '.text', '.html', '.htm']
const MAX_ATTACH_BYTES = 25 * 1024 * 1024
export const PASTED_NAME = 'Pasted text'

/** The hunt already on this document: its case when it has one, else its run, and where it stands. */
export type RunningHunt = { caseId?: string; runId?: string; status?: string }

/** A hypothesis proposed from the attached document by the coverage check; null until asked. */
export type HuntProposal =
  | { status: 'checking' }
  | { status: 'proposed'; hypothesis: string; subjects?: Record<string, string[]>; approve?: boolean; running?: RunningHunt }
  | { status: 'none'; reason: string }

/** The file attached in the bar: being read, refused with a reason, or read. */
export type HuntAttachment =
  | { status: 'reading'; name: string }
  | { status: 'refused'; name: string; reason: string }
  | {
      status: 'ready'
      name: string
      file: File
      pasted: boolean
      pages: number
      condensed: boolean
      /** The text a run or Ask Vigil reads: extracted, or condensed to fit. */
      text: string
      proposal: HuntProposal | null
    }

/** Why a file is not worth sending, or null. The server holds the same two rules. */
export function attachRefusal(file: { name: string; size: number }): string | null {
  const dot = file.name.lastIndexOf('.')
  const suffix = dot < 0 ? '' : file.name.slice(dot).toLowerCase()
  if (!ATTACH_TYPES.includes(suffix)) {
    return `${suffix || 'That'} files cannot be attached. Attach a PDF, DOCX, PPTX, XLSX, PNG, JPG, Markdown, text or HTML file.`
  }
  if (file.size > MAX_ATTACH_BYTES) return `File too large. The most that can be attached is ${MAX_ATTACH_BYTES / 1024 / 1024} MB.`
  return null
}

/** Why the typed command takes no file (the paperclip's tooltip), or null when it can take one. */
export function attachBlockedBy(query: string): string | null {
  const line = parseLine(query)
  if (line?.kind === 'later') return `${line.command.name} is not available yet and takes no file`
  return line?.kind === 'command' && !line.command.takesFile ? `${line.command.name} takes a case, not a file` : null
}

/** Over this many characters, or on more than one line, a paste attaches as a file instead of landing in the input. */
export const PASTE_ATTACH_CHARS = 500

/** The hypothesis a run starts with: what was typed, else what the document proposed. */
export function huntHypothesis(arg: string, attachment: HuntAttachment | null): string {
  const typed = arg.trim()
  if (typed) return typed
  return attachment?.status === 'ready' && attachment.proposal?.status === 'proposed' ? attachment.proposal.hypothesis : ''
}

/** The hunt already running on the attached document, when its proposal says so. */
export function runningHunt(attachment: HuntAttachment | null): RunningHunt | null {
  return attachment?.status === 'ready' && attachment.proposal?.status === 'proposed' ? attachment.proposal.running ?? null : null
}

/** The coverage check's answer as a proposal. A report already being hunted still gets one, and names that hunt. */
export function proposalFrom(answer: {
  status?: string
  in_flight?: { run_id?: string | null; case_id?: string | null; status?: string | null }[]
  proposal?: { hypothesis?: unknown; hypothesis_subjects?: Record<string, string[]>; approve_hypotheses?: boolean } | null
}): HuntProposal {
  const hypothesis = answer.proposal?.hypothesis
  if (typeof hypothesis === 'string' && hypothesis.trim()) {
    // Newest first across both states, so a paused one can lead a running one: name the live hunt.
    const hunts = answer.in_flight ?? []
    const hunt = hunts.find((row) => row.status === 'running') ?? hunts[0]
    return {
      status: 'proposed',
      hypothesis,
      subjects: answer.proposal?.hypothesis_subjects,
      approve: answer.proposal?.approve_hypotheses,
      ...(answer.status === 'running' && { running: { caseId: hunt?.case_id ?? undefined, runId: hunt?.run_id ?? undefined, status: hunt?.status ?? undefined } }),
    }
  }
  if (answer.status === 'running') {
    const hunts = answer.in_flight ?? []
    const state = hunts.some((row) => row.status === 'running') || hunts.length === 0 ? 'running' : 'paused'
    return { status: 'none', reason: `A hunt on this document is ${state}. Type a hypothesis to start another.` }
  }
  return { status: 'none', reason: 'No hypothesis could be proposed from this document. Type one to hunt with it.' }
}

/** The note for a hunt already on the document: its real state, and that Run starts another. */
export function huntStateNote(hunt: RunningHunt): string {
  const state = hunt.status === 'paused' ? 'paused' : hunt.status === 'running' ? 'running' : 'in progress'
  return `A hunt on this document is ${state}${hunt.caseId ? ` (case ${hunt.caseId})` : ''}. Run starts a new one.`
}

/** A case title from a hypothesis: whole words only, never past ``max`` characters. */
export function huntTitle(hypothesis: string, max = HUNT_TITLE_MAX): string {
  const text = hypothesis.trim().replace(/\s+/g, ' ')
  if (text.length <= max) return text
  const cut = text.slice(0, max + 1)
  // The character past the limit is a space when the limit lands on a word end.
  const boundary = cut.lastIndexOf(' ')
  const words = boundary > 0 ? cut.slice(0, boundary) : text.slice(0, max)
  return words.replace(/[\s,;:.\-–—]+$/, '')
}

/** The destination chip's text. */
export function tagText(row: PaletteRow): string {
  switch (row.dest) {
    case 'Case':
      return 'Opens case'
    case 'Alert':
    case 'Page':
      return 'Opens page'
    case 'Recent':
      return 'Search'
    case 'Ask':
      return 'Opens Ask Vigil'
    case 'Command':
      return row.disabled ? 'Later' : 'Command'
    case 'Argument':
      return 'Use'
  }
}

const MATCH_DESC: Record<MatchType, string> = {
  case: 'Matched title or description',
  comment: 'Matched a comment',
  evidence: 'Matched evidence',
  alert: 'Matched an alert',
}

function joinParts(parts: (string | null | undefined)[]): string {
  return parts.filter((part) => part).join(' · ')
}

function caseDesc(hit: Hit): string {
  // A title that does not name the query needs its reason: it matched an alert.
  if (hit.match_type === 'alert') return joinParts([hit.priority, hit.status, MATCH_DESC.alert])
  const state = joinParts([hit.priority, hit.status])
  return state || (hit.match_type ? MATCH_DESC[hit.match_type] : '')
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

export function useJiraReadiness(): JiraReadiness {
  const [jira, setJira] = useState(() => jiraReadiness(null))
  useEffect(() => {
    let live = true
    configApi
      .getIntegrations()
      .then((res) => {
        if (live) setJira(jiraReadiness(res.data))
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])
  return jira
}

export type CommandLine = { kind: 'command'; command: LiveCommand; word: string }

/** A line that starts with a slash: a live command, a Later one, or neither. */
export type Line =
  | CommandLine
  | { kind: 'later'; command: LaterCommand }
  | { kind: 'unknown'; token: string; tail: string; near: LiveCommand[] }

const NEAR_DISTANCE = 2

function editDistance(a: string, b: string): number {
  let row = Array.from({ length: b.length + 1 }, (_, j) => j)
  for (let i = 1; i <= a.length; i += 1) {
    const next = [i]
    for (let j = 1; j <= b.length; j += 1) {
      next[j] = Math.min(row[j] + 1, next[j - 1] + 1, row[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1))
    }
    row = next
  }
  return row[b.length]
}

/** What a mistyped token was probably meant to be, nearest first. A second slash is a path, not a typo. */
function nearCommands(token: string): LiveCommand[] {
  if (token.indexOf('/', 1) > 0) return []
  return LIVE_COMMANDS
    .map((command) => ({ command, distance: editDistance(token, command.name) }))
    .filter(({ distance }) => distance <= NEAR_DISTANCE)
    .sort((a, b) => a.distance - b.distance)
    .map(({ command }) => command)
}

export function parseLine(query: string): Line | null {
  const trimmed = query.trim()
  if (!trimmed.startsWith('/')) return null
  const token = trimmed.split(/\s+/)[0]
  const rest = trimmed.slice(token.length).trim()
  const lower = token.toLowerCase()
  const command = COMMANDS.find((c) => c.name === lower)
  if (command?.later) return { kind: 'later', command }
  if (command) return { kind: 'command', command, word: rest }
  return { kind: 'unknown', token, tail: rest, near: commandsFor(lower).length > 0 ? [] : nearCommands(lower) }
}

/** The ids a word could be, from GET /search, or the first few a list shows when nothing is typed. */
export interface RefLookup {
  kind: RefKind
  word: string
  failed: boolean
  exact: Hit | null
  hits: Hit[]
}

export function lookupWanted(line: Line | null): { kind: RefKind; word: string } | null {
  if (line?.kind !== 'command') return null
  const { args } = line.command
  const kind = args.kind === 'ref' ? args.ref : args.rows
  return kind ? { kind, word: line.word } : null
}

export async function fetchRefs(kind: RefKind, word: string, signal: AbortSignal): Promise<Pick<RefLookup, 'exact' | 'hits'>> {
  if (word) {
    const { data } = await searchApi.query(word, signal)
    return { exact: (kind === 'case' ? data.case_exact : data.finding_exact) ?? null, hits: argumentHits(kind, data) }
  }
  // Nothing typed yet: the first few the Cases list or the alert feed shows.
  if (kind === 'case') {
    const { data } = await casesApi.getAll({ limit: 5 })
    return { exact: null, hits: (data.cases ?? []).map((c) => ({ id: c.case_id, title: c.title, priority: c.priority })) }
  }
  const { data } = await findingsApi.getAll({ limit: 5 }, signal)
  const findings = (data as { findings?: { finding_id: string; title?: string | null; description?: string | null; data_source?: string | null; severity?: string | null }[] }).findings ?? []
  return { exact: null, hits: findings.map((f) => ({ id: f.finding_id, title: f.title || f.description || f.finding_id, data_source: f.data_source, severity: f.severity })) }
}

export type Binding =
  | { state: 'off' | 'missing' | 'searching' | 'unresolved'; reason: string }
  | { state: 'bound'; target: Bound; line: string; note?: string }

function resolveRef({ word }: CommandLine, ref: RefKind, lookup: RefLookup | null, env: BindEnv): Bound | Exclude<Binding, { state: 'bound' }> {
  if (!word) {
    if (ref === 'case' && env.openCaseId) return { id: env.openCaseId, text: '' }
    return { state: 'missing', reason: `Add ${ref === 'alert' ? 'an alert' : 'a case'} id` }
  }
  if (lookup?.word !== word) return { state: 'searching', reason: `Looking for ${ref} ${word}…` }
  // The search itself failed, so nobody knows. The server answers for the id, as it would have.
  if (lookup.failed) return { id: word, text: '' }
  if (lookup.exact?.id) return { id: lookup.exact.id, text: '' }
  return { state: 'unresolved', reason: `No ${ref} ${word}.${lookup.hits.length > 0 ? ' Pick one below.' : ''}` }
}

export function bindLine(line: CommandLine, lookup: RefLookup | null, env: BindEnv): Binding {
  const { command, word } = line
  // A command that reads the file waits for it, and a refused one stops it.
  if (command.takesFile) {
    if (env.attachment?.status === 'reading') return { state: 'off', reason: `Reading ${env.attachment.name}…` }
    if (env.attachment?.status === 'refused') return { state: 'off', reason: env.attachment.reason }
  }
  const resolved = command.args.kind === 'ref' ? resolveRef(line, command.args.ref, lookup, env) : { id: '', text: word }
  if ('state' in resolved) return resolved
  const reason = command.blocked?.(resolved, env)
  if (reason) return { state: 'missing', reason }
  return { state: 'bound', target: resolved, ...command.preview(resolved, env) }
}

/** Everything Enter, Tab and the footer read. `active` is null until the person presses an arrow. */
export interface BarView {
  query: string
  line: Line | null
  binding: Binding | null
  rows: PaletteRow[]
  active: number | null
  running: boolean
}

export type KeyAction =
  | { kind: 'none' }
  | { kind: 'default' }
  | { kind: 'run' }
  | { kind: 'ask' }
  | { kind: 'row'; index: number; label: 'open' | 'use' | 'complete' }

const selectable = (row: PaletteRow) => !row.disabled && !row.echo

function rowAction(rows: PaletteRow[], index: number): KeyAction {
  const row = rows[index]
  return { kind: 'row', index, label: row.dest === 'Command' ? 'complete' : row.fill !== undefined ? 'use' : 'open' }
}

/** First matching row wins, unless the person moved to one. */
export function targetRow({ binding, rows, active }: BarView): number {
  // Rows still showing for an older word are not an answer, even under the cursor.
  if (binding?.state === 'searching') return -1
  if (active !== null && rows[active] && selectable(rows[active])) return active
  // A bound line runs on Enter.
  if (binding?.state === 'bound') return -1
  return rows.findIndex(selectable)
}

export function keyAction(key: 'Enter' | 'Tab', view: BarView): KeyAction {
  const { query, line, binding, rows, running } = view
  const fallback: KeyAction = key === 'Enter' ? { kind: 'none' } : { kind: 'default' }
  if (running) return fallback
  const index = targetRow(view)
  if (!line && key === 'Tab') return query.trim() ? { kind: 'ask' } : { kind: 'default' }
  if (index >= 0) return rowAction(rows, index)
  // A pasted /var/log/x is a real question. Nothing listed means nothing here claims it.
  if (line?.kind === 'unknown' && rows.length === 0 && key === 'Tab') return { kind: 'ask' }
  if (binding?.state === 'bound' && key === 'Enter') return { kind: 'run' }
  return fallback
}

function commandsFor(query: string): CommandDef[] {
  const q = query.trim().toLowerCase()
  if (!q) return LIVE_COMMANDS
  if (q.startsWith('/')) {
    const token = q.split(/\s+/)[0]
    return COMMANDS.filter((command) => {
      if (command.name.startsWith('/')) return command.name.toLowerCase().startsWith(token)
      return token === '/' || token === 'custom' || token.startsWith('custom')
    })
  }
  return COMMANDS.filter((command) => command.id !== 'custom' && command.name.toLowerCase().includes(q))
}

function commandRow(command: CommandDef, tail = ''): PaletteRow {
  const custom = command.id === 'custom'
  return {
    key: `cmd:${command.id}`,
    label: custom ? command.desc : command.name,
    hint: command.hint,
    dest: 'Command',
    section: custom ? 'Custom commands' : 'Commands',
    icon: custom ? 'plus' : 'bolt',
    desc: custom ? 'Agents & workflows › Commands' : command.desc,
    disabled: command.later,
    commandId: command.id,
    ...(!command.later && { fill: `${command.name} ${tail}` }),
  }
}

function slashRows(line: Line): PaletteRow[] {
  if (line.kind === 'later') return [commandRow(line.command)]
  if (line.kind === 'command') return [{ ...commandRow(line.command, line.word), echo: true }]
  if (line.near.length > 0) return line.near.map((command) => ({ ...commandRow(command, line.tail), desc: `Did you mean ${command.name}?` }))
  // COMMANDS lists the custom row last, so it lands in its own section.
  return commandsFor(line.token).map((command) => commandRow(command, line.tail))
}

function recentRow(text: string): PaletteRow {
  return {
    key: `recent:${text}`,
    label: text,
    hint: '',
    dest: 'Recent',
    section: 'Recent searches',
    icon: 'clock',
    desc: '',
    disabled: false,
    recent: text,
  }
}

function askRow(query: string): PaletteRow {
  const q = query.trim()
  return {
    key: 'ask:vigil',
    label: q ? `Ask Vigil: “${q}”` : 'Ask Vigil about this page',
    hint: '',
    dest: 'Ask',
    section: 'Ask Vigil',
    icon: 'sparkle',
    desc: q ? 'Opens chat with your question' : 'Opens chat with this page as context',
    disabled: false,
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
    section: 'Cases',
    icon: 'folder',
    desc: caseDesc(hit),
    disabled: false,
    caseId: hit.id,
  })
}

const AUTOMATION: { kind: 'workflows' | 'agents' | 'skills'; noun: string; icon: IconName; search: (id: string) => string }[] = [
  { kind: 'workflows', noun: 'Workflow', icon: 'flow', search: (id) => `?workflow=${id}` },
  { kind: 'agents', noun: 'Agent', icon: 'bot', search: (id) => `?tab=agents&agent=${id}` },
  { kind: 'skills', noun: 'Skill', icon: 'doc', search: (id) => `?tab=skills&skill=${id}` },
]

export function buildRows(
  query: string,
  hits: SearchHits | null,
  recents: string[],
  boards: BoardLink[],
): PaletteRow[] {
  const q = query.trim()
  const ql = q.toLowerCase()
  if (!q) return [...recents.map(recentRow), ...LIVE_COMMANDS.map((command) => commandRow(command)), askRow('')]

  const line = parseLine(q)
  if (line) return slashRows(line)

  const cases: PaletteRow[] = []
  const seen = new Set<string>()
  if (hits?.case_exact) pushCase(cases, seen, hits.case_exact)
  const alerts: PaletteRow[] = []
  const seenAlerts = new Set<string>()
  for (const hit of [hits?.finding_exact, ...(hits?.alerts ?? [])]) {
    if (!hit?.id || seenAlerts.has(hit.id)) continue
    seenAlerts.add(hit.id)
    alerts.push({
      key: `finding:${hit.id}`,
      label: hit.title || hit.id,
      hint: hit.id,
      dest: 'Alert',
      section: 'Alerts',
      icon: 'alert',
      desc: joinParts([hit.data_source, hit.severity]),
      disabled: false,
      findingId: hit.id,
    })
  }
  for (const hit of hits?.cases ?? []) pushCase(cases, seen, hit)
  const pages: PaletteRow[] = boards
    .filter((board) => board.label.toLowerCase().includes(ql))
    .map((board) => ({
      key: `page:${board.key}`,
      label: board.label,
      hint: '',
      dest: 'Page' as const,
      section: 'Pages' as const,
      icon: board.icon,
      desc: board.desc,
      disabled: false,
      page: board.key,
    }))
  // Deep links into Agents & workflows: the workflow, or the agent or skill on its tab.
  const automation: PaletteRow[] = AUTOMATION.flatMap(({ kind, noun, icon, search }) =>
    (hits?.[kind] ?? []).map((hit) => ({
      key: `${kind}:${hit.id}`,
      label: hit.title,
      hint: '',
      dest: 'Page' as const,
      section: 'Agents & workflows' as const,
      icon,
      desc: joinParts([noun, hit.description]),
      disabled: false,
      page: 'workflows',
      search: search(encodeURIComponent(hit.id)),
    })),
  )
  const commands = commandsFor(q).map((command) => commandRow(command))
  const recent = recents.filter((text) => text.toLowerCase().includes(ql)).map(recentRow)
  // An exact finding id with no exact case id puts Alerts first.
  const alertsFirst = Boolean(hits?.finding_exact?.id) && !hits?.case_exact
  return [
    ...(alertsFirst ? [...alerts, ...cases] : [...cases, ...alerts]),
    ...pages,
    ...automation,
    ...commands,
    ...recent,
    askRow(q),
  ]
}

/** The rows under a command line that put an id on it. A settled id needs none. */
export function argumentRows({ command, word }: CommandLine, binding: Binding, lookup: RefLookup | null): PaletteRow[] {
  if (!lookup || binding.state === 'off') return []
  if (binding.state === 'bound' && command.args.kind === 'ref' && word) return []
  const { kind } = lookup
  const seen = new Set<string>()
  return lookup.hits.flatMap((hit) => {
    if (!hit.id || seen.has(hit.id)) return []
    seen.add(hit.id)
    return [{
      key: `arg:${hit.id}`,
      label: hit.title || hit.id,
      hint: hit.id,
      dest: 'Argument' as const,
      section: kind === 'case' ? 'Cases' as const : 'Alerts' as const,
      icon: kind === 'case' ? 'folder' as const : 'alert' as const,
      desc: kind === 'case' ? caseDesc(hit) : joinParts([hit.data_source, hit.severity]),
      disabled: false,
      fill: `${command.name} ${hit.id}`,
    }]
  })
}

/** The argument rows a search answer holds, the exact id first. */
function argumentHits(kind: RefKind, hits: SearchHits): Hit[] {
  const found = kind === 'case' ? [hits.case_exact, ...(hits.cases ?? [])] : [hits.finding_exact, ...(hits.alerts ?? [])]
  return found.filter((hit): hit is Hit => Boolean(hit))
}

export function moveEnabled(rows: PaletteRow[], from: number, direction: 1 | -1): number {
  if (rows.length === 0) return -1
  // Nothing highlighted yet: down lands on the first row, up on the last.
  let index = from < 0 && direction < 0 ? 0 : from
  for (let step = 0; step < rows.length; step += 1) {
    index = (index + direction + rows.length) % rows.length
    if (selectable(rows[index])) return index
  }
  return from
}
