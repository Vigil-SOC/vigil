/**
 * The command bar's commands, one record each: what it takes, what its preview says,
 * and what it does. A line goes query -> parseLine -> lookup -> bindLine -> keyAction,
 * all derived in render, so nothing here can drift from what is typed.
 */

import { useEffect, useState } from 'react'
import { casesApi, configApi, findingsApi, searchApi, workflowApi } from '../services/api'
import type { Schema } from '../services/apiTypes'
import type { IconName } from '../shared/icons'
import { mapApiWorkflow } from '../data/mappers'
import { prettyHandle, type Workflow } from '../data/appData'
import { closurePayload, closureProblem } from '../screens/cases/ClosureFields'
import { CLOSURE_CATEGORIES, notifyCasesChanged } from '../screens/cases/useCases'
import { errorText } from '../shared/errorText'
import type { ConsoleScreenGoOptions } from '../shared/types'
import type { ToastKind } from './toast'

type RefKind = 'case' | 'alert' | 'workflow'

interface Choice {
  value: string
  label: string
  desc?: string
}

/**
 * What a command takes. A ref only ever binds to an exact id, so a search word cannot reach an API.
 * With a text slot the first word is the ref and the rest is text; without one the whole remainder is the ref.
 */
type Args =
  | { kind: 'free'; rows?: 'alert' }
  | { kind: 'ref'; ref: RefKind; text?: true; choices?: readonly Choice[] }

/** A ref a lookup found: a case, an alert, or a workflow and whether it can run. */
type Ref = Hit & { huntLike?: boolean; off?: string }

/** What a line resolved to. Only bindLine makes one, and run and fused take nothing else. */
interface Bound {
  id: string
  text: string
  /** The line named the id itself, rather than leaning on the open case. */
  named: boolean
  ref?: Ref
}

/** What binding a line reads, beyond the line itself. */
interface BindEnv {
  openCaseId: string | null
  jira: JiraReadiness
  attachment: HuntAttachment | null
  can: (permission: string) => boolean
}

/** What a run may reach in the console around the bar. */
interface RunContext extends BindEnv {
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

/** A change that waits out the undo fuse; nothing is sent until it ends. */
interface FusedAction {
  text: string
  doneText: string
  failText: string
  commit: () => Promise<unknown>
}

interface LiveBase extends CommandText {
  later: false
  /** On the empty palette and the Home chips. */
  featured?: true
  /** The permissions the server gates this on. Without one the row stays, disabled with the reason. */
  needs: string[]
  /** It reads an attached file; the paperclip is off for the rest. */
  takesFile?: boolean
  /** The workflow it starts, when it starts one. */
  workflowId?: string
  args: Args
  /** Why a resolved line cannot run yet. */
  blocked?: (target: Bound, env: BindEnv) => string | null
  preview: (target: Bound, env: BindEnv) => { line: string; note?: string }
}

type LiveCommand = LiveBase & (
  | { run: (target: Bound, ctx: RunContext) => Promise<void>; fused?: never }
  | { fused: (target: Bound) => FusedAction; run?: never }
)

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

// The console's own close asks for a category; the status edit records this one, closed with no reason stated.
const UNSPECIFIED = { value: 'unspecified', label: 'Unspecified', desc: 'Closed, no reason stated' } as const
const CLOSE_AS = [...CLOSURE_CATEGORIES, UNSPECIFIED]

/** The category and reason after the case on a /close line. No category typed is unspecified. */
function closeAs(text: string) {
  const [typed = '', ...rest] = text.split(/\s+/)
  return { typed, reason: rest.join(' '), category: typed ? CLOSE_AS.find((c) => c.value === typed) : UNSPECIFIED }
}

function workflowOff(workflow: Workflow): string | undefined {
  if (!workflow.enabled) return `${workflow.name} is off`
  const blocked = (workflow.blockedBy ?? []).map(prettyHandle)
  return blocked.length > 0 ? `${workflow.name} can’t run. ${blocked.join(', ')} ${blocked.length === 1 ? 'is' : 'are'} off` : undefined
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
    featured: true,
    needs: ['automation.read', 'ai_chat.use'],
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
    featured: true,
    needs: ['automation.read', 'ai_chat.use'],
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
    featured: true,
    needs: ['cases.read'],
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
    featured: true,
    needs: ['ai_chat.use'],
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
    featured: true,
    needs: ['cases.read'],
    args: { kind: 'ref', ref: 'case' },
    blocked: (_target, { jira }) => jira.gap,
    preview: ({ id }, { jira }) => ({ line: `Export case ${id} to Jira project ${jira.projectKey}` }),
    run: async ({ id }, ctx) => {
      ctx.notify('ok', await casesApi.exportJira(id, ctx.jira.projectKey))
    },
  },
  {
    id: 'note',
    name: '/note',
    hint: '<case> <text>',
    desc: 'Add a note to a case',
    runs: 'Adds a comment to the case',
    later: false,
    needs: ['cases.write'],
    args: { kind: 'ref', ref: 'case', text: true },
    blocked: ({ text }) => (text ? null : 'Add the note'),
    preview: ({ id, text }) => ({ line: `Add a note to case ${id}: ${text}` }),
    run: async ({ id, text }, ctx) => {
      await casesApi.addComment(id, { content: text })
      notifyCasesChanged()
      ctx.notify('ok', `Note added to case ${id}`)
    },
  },
  {
    id: 'assign',
    name: '/assign',
    hint: '<case> <user>',
    desc: 'Hand a case to a person, by username or email',
    runs: 'Changes the case owner, with undo',
    later: false,
    needs: ['cases.write', 'cases.assign'],
    args: { kind: 'ref', ref: 'case', text: true },
    blocked: ({ text }) => (text ? null : 'Add a username or email'),
    preview: ({ id, text }) => ({ line: `Assign case ${id} to ${text}` }),
    fused: ({ id, text }) => ({
      text: `Assigning case ${id} to ${text}`,
      doneText: `Case ${id} assigned to ${text}`,
      failText: `Couldn’t assign case ${id}`,
      commit: () => casesApi.update(id, { assignee: text }),
    }),
  },
  {
    id: 'close',
    name: '/close',
    hint: '<case> [category]',
    desc: 'Close a case, with what it turned out to be',
    runs: 'Closes the case, with undo',
    later: false,
    needs: ['cases.write'],
    args: { kind: 'ref', ref: 'case', text: true, choices: CLOSE_AS },
    blocked: ({ text }) => {
      const { typed, reason, category } = closeAs(text)
      if (!category) return `No category ${typed}. Use ${CLOSURE_CATEGORIES.map((c) => c.value).join(', ')} or ${UNSPECIFIED.value}.`
      return category.value === 'unspecified' ? null : closureProblem(category.value, reason)
    },
    preview: ({ id, text }) => {
      const { category, reason } = closeAs(text)
      return { line: `Close case ${id} as ${category?.label.toLowerCase()}${reason ? `: ${reason}` : ''}` }
    },
    fused: ({ id, text }) => {
      const { category, reason } = closeAs(text)
      const label = category?.label.toLowerCase()
      return {
        text: `Closing case ${id} as ${label}`,
        doneText: `Case ${id} closed as ${label}`,
        failText: `Couldn’t close case ${id}`,
        commit: () => casesApi.closeCase(id, !category || category.value === 'unspecified'
          ? { closure_category: 'unspecified', ...(reason && { closure_notes: reason }) }
          : closurePayload(category.value, reason)),
      }
    },
  },
  {
    id: 'reopen',
    name: '/reopen',
    hint: '<case>',
    desc: 'Reopen a closed case',
    runs: 'Reopens the case, with undo',
    later: false,
    needs: ['cases.write'],
    args: { kind: 'ref', ref: 'case' },
    preview: ({ id }) => ({ line: `Reopen case ${id}` }),
    fused: ({ id }) => ({
      text: `Reopening case ${id}`,
      doneText: `Case ${id} reopened`,
      failText: `Couldn’t reopen case ${id}`,
      commit: () => casesApi.update(id, { status: 'open' }),
    }),
  },
  {
    id: 'stop',
    name: '/stop',
    hint: '<case> [reason]',
    desc: 'Stop the live run on a case and reject its pending approvals',
    runs: 'Cancels the run',
    later: false,
    needs: ['automation.read', 'ai_decisions.approve'],
    args: { kind: 'ref', ref: 'case', text: true },
    preview: ({ id, text }) => ({ line: `Stop the live run on case ${id}${text ? `: ${text}` : ''}`, note: 'Pending approvals on the run get rejected.' }),
    run: async ({ id, text }, ctx) => {
      const runId = (await casesApi.getById(id)).data.investigations?.find((item) => item.live)?.run_id
      if (!runId) {
        ctx.notify('info', `Case ${id} has no live run to stop`)
        return
      }
      await workflowApi.cancelRun(runId, text || 'Stopped from the command bar')
      notifyCasesChanged()
      ctx.notify('ok', `Stopped the run on case ${id}`)
    },
  },
  {
    id: 'run',
    name: '/run',
    hint: '<workflow> <input>',
    desc: 'Start any workflow that is on',
    runs: 'The workflow you name',
    later: false,
    needs: ['automation.read', 'ai_chat.use'],
    args: { kind: 'ref', ref: 'workflow', text: true },
    blocked: ({ text, ref }) => (text ? null : ref?.huntLike ? 'Add a hypothesis' : 'Add a finding id or context'),
    preview: ({ id, text }) => ({ line: `Run ${id}: ${text}` }),
    run: async ({ id, text, ref }, ctx) => {
      const started = await workflowApi.execute(id, ref?.huntLike ? { hypothesis: text } : await findingOrContext(text))
      const { case_id: caseId, run_id: runId } = started.data
      if (caseId) ctx.onOpenCase(caseId)
      else if (runId) ctx.onGo('workflows', { search: `?run=${encodeURIComponent(runId)}` })
      ctx.notify('ok', `Started ${id}`)
    },
  },
  {
    id: 'noise',
    name: '/noise',
    hint: '<alert>',
    desc: 'Dismiss an alert as noise',
    runs: 'Marks the alert as noise, with undo',
    later: false,
    needs: ['findings.write'],
    args: { kind: 'ref', ref: 'alert' },
    preview: ({ id }) => ({ line: `Mark alert ${id} as noise` }),
    fused: ({ id }) => ({
      text: `Marking alert ${id} as noise`,
      doneText: `Alert ${id} marked as noise`,
      failText: `Couldn’t mark alert ${id} as noise`,
      commit: () => findingsApi.markNoise(id),
    }),
  },
  {
    id: 'decisions',
    name: '/decisions',
    hint: '',
    desc: 'See the cases waiting on a person',
    runs: 'Opens Cases on Needs you',
    later: false,
    // The Cases screen's own gate: Home's Needs you section is closed to anyone without ai_decisions.approve.
    needs: ['cases.read'],
    args: { kind: 'free' },
    preview: () => ({ line: 'Open the cases that need you' }),
    run: async (_target, ctx) => {
      ctx.onGo('cases', { search: '?needs_you=1' })
    },
  },
  {
    id: 'case',
    name: '/case',
    hint: '<title>',
    desc: 'Open a new case with no alerts on it yet',
    runs: 'Creates a case',
    later: false,
    needs: ['cases.write'],
    args: { kind: 'free' },
    blocked: ({ text }) => (text ? null : 'Add a title'),
    preview: ({ text }) => ({ line: `Create a case: ${text}` }),
    run: async ({ text }, ctx) => {
      const { data } = await casesApi.create({ title: text, description: '', finding_ids: [], priority: 'medium', status: 'open' })
      notifyCasesChanged()
      if (!data.case_id) throw new Error('The case was created without an id')
      ctx.onOpenCase(data.case_id)
      ctx.notify('ok', `Case created: ${text}`)
    },
  },
  { id: 'hold', name: '/hold', hint: '<case or kind>', desc: 'Hold a case, or every case of one kind', runs: '—', later: true },
  { id: 'isolate', name: '/isolate', hint: '<host>', desc: 'Isolate a host. Irreversible, always asks you to confirm', runs: '—', later: true },
  { id: 'phish', name: '/phish', hint: '<message or sender>', desc: 'Run Phishing triage on a reported email', runs: '—', later: true },
  { id: 'custom', name: 'Custom commands', hint: '', desc: 'Create a command for any workflow', runs: '—', later: true },
]

export const LIVE_COMMANDS = COMMANDS.filter((c): c is LiveCommand => !c.later)
export const FEATURED_COMMANDS = LIVE_COMMANDS.filter((c) => c.featured)

const LATER_WHY = 'Coming in a later release'

/** Why this role cannot run a command, or null. */
function deniedBy(command: LiveCommand, can: BindEnv['can']): string | null {
  const missing = command.needs.find((permission) => !can(permission))
  return missing ? `Your role can’t run ${command.name}. It needs ${missing}.` : null
}

type DestTag = 'Case' | 'Alert' | 'Page' | 'Command' | 'Recent' | 'Ask' | 'Argument'

export type Section =
  | 'Recent searches'
  | 'Commands'
  | 'Custom commands'
  | 'Cases'
  | 'Alerts'
  | 'Pages'
  | 'Agents & workflows'
  | 'Close as'
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
  /** Why a disabled row is disabled; its tooltip. */
  why?: string
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
  if (line?.kind !== 'command' || line.command.takesFile) return null
  const { args, name } = line.command
  if (args.kind !== 'ref') return `${name} takes no file`
  return `${name} takes ${args.ref === 'alert' ? 'an alert' : `a ${args.ref}`}, not a file`
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
      if (!row.disabled) return 'Command'
      return row.why === LATER_WHY ? 'Later' : 'No access'
    case 'Argument':
      return row.disabled ? 'Off' : 'Use'
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

type CommandLine = { kind: 'command'; command: LiveCommand; word: string; text: string }

/** A line that starts with a slash: a live command, a Later one, or neither. */
type Line =
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
  if (command) {
    if (command.args.kind !== 'ref' || !command.args.text) return { kind: 'command', command, word: rest, text: '' }
    const [word = ''] = rest.split(/\s+/)
    return { kind: 'command', command, word, text: rest.slice(word.length).trim() }
  }
  return { kind: 'unknown', token, tail: rest, near: commandsFor(lower).length > 0 ? [] : nearCommands(lower) }
}

/** The ids a word could be, from GET /search, or the first few a list shows when nothing is typed. */
export interface RefLookup {
  kind: RefKind
  word: string
  failed: boolean
  exact: Ref | null
  hits: Ref[]
}

export function lookupWanted(line: Line | null, can: BindEnv['can']): { kind: RefKind; word: string } | null {
  if (line?.kind !== 'command' || deniedBy(line.command, can)) return null
  const { args } = line.command
  const kind = args.kind === 'ref' ? args.ref : args.rows
  return kind ? { kind, word: line.word } : null
}

export async function fetchRefs(kind: RefKind, word: string, signal: AbortSignal): Promise<Pick<RefLookup, 'exact' | 'hits'>> {
  if (kind === 'workflow') {
    const { data } = await workflowApi.listAll()
    const q = word.toLowerCase()
    const refs: Ref[] = ((data as { workflows?: Parameters<typeof mapApiWorkflow>[0][] }).workflows ?? []).map(mapApiWorkflow).map((workflow) => ({
      id: workflow.id,
      title: workflow.name,
      description: workflow.desc,
      huntLike: workflow.huntLike,
      off: workflowOff(workflow),
    }))
    return {
      exact: refs.find((ref) => ref.id === word) ?? null,
      hits: refs.filter((ref) => ref.id.includes(q) || ref.title.toLowerCase().includes(q)),
    }
  }
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

type Binding =
  | { state: 'off' | 'searching' | 'unresolved'; reason: string }
  | { state: 'missing'; reason: string; target?: Bound }
  | { state: 'bound'; target: Bound; line: string; note?: string }

function resolveRef({ word, text }: CommandLine, args: Extract<Args, { kind: 'ref' }>, lookup: RefLookup | null, env: BindEnv): Bound | Exclude<Binding, { state: 'bound' }> {
  const { ref } = args
  const openCase = ref === 'case' ? env.openCaseId : null
  if (!word) {
    if (openCase) return { id: openCase, text: '', named: false }
    return { state: 'missing', reason: ref === 'workflow' ? 'Add a workflow' : `Add ${ref === 'alert' ? 'an alert' : 'a case'} id` }
  }
  if (lookup?.word !== word) return { state: 'searching', reason: `Looking for ${ref} ${word}…` }
  // The search itself failed, so nobody knows. The server answers for the id, as it would have.
  if (lookup.failed) return { id: word, text, named: true }
  if (lookup.exact?.off) return { state: 'off', reason: lookup.exact.off }
  if (lookup.exact?.id) return { id: lookup.exact.id, text, named: true, ref: lookup.exact }
  // Not an id, so with a case open it is the start of what was said about that case.
  if (args.text && openCase) return { id: openCase, text: `${word} ${text}`.trim(), named: false }
  return { state: 'unresolved', reason: `No ${ref} ${word}.${lookup.hits.length > 0 ? ' Pick one below.' : ''}` }
}

export function bindLine(line: CommandLine, lookup: RefLookup | null, env: BindEnv): Binding {
  const { command, word } = line
  const denied = deniedBy(command, env.can)
  if (denied) return { state: 'off', reason: denied }
  // A command that reads the file waits for it, and a refused one stops it.
  if (command.takesFile) {
    if (env.attachment?.status === 'reading') return { state: 'off', reason: `Reading ${env.attachment.name}…` }
    if (env.attachment?.status === 'refused') return { state: 'off', reason: env.attachment.reason }
  }
  const resolved = command.args.kind === 'ref' ? resolveRef(line, command.args, lookup, env) : { id: '', text: word, named: false }
  if ('state' in resolved) return resolved
  const reason = command.blocked?.(resolved, env)
  if (reason) return { state: 'missing', reason, target: resolved }
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

type KeyAction =
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

function commandRow(command: CommandDef, can: BindEnv['can'], tail = ''): PaletteRow {
  const custom = command.id === 'custom'
  const why = command.later ? LATER_WHY : deniedBy(command, can) ?? undefined
  return {
    key: `cmd:${command.id}`,
    label: custom ? command.desc : command.name,
    hint: command.hint,
    dest: 'Command',
    section: custom ? 'Custom commands' : 'Commands',
    icon: custom ? 'plus' : 'bolt',
    desc: custom ? 'Agents & workflows › Commands' : !command.later && why ? why : command.desc,
    disabled: why !== undefined,
    why,
    commandId: command.id,
    ...(!why && { fill: `${command.name} ${tail}` }),
  }
}

function slashRows(line: Line, can: BindEnv['can']): PaletteRow[] {
  if (line.kind === 'later') return [commandRow(line.command, can)]
  if (line.kind === 'command') return [{ ...commandRow(line.command, can, `${line.word} ${line.text}`.trim()), echo: true }]
  if (line.near.length > 0) {
    return line.near.map((command) => {
      const row = commandRow(command, can, line.tail)
      return row.disabled ? row : { ...row, desc: `Did you mean ${command.name}?` }
    })
  }
  // COMMANDS lists the custom row last, so it lands in its own section.
  return commandsFor(line.token).map((command) => commandRow(command, can, line.tail))
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
  can: BindEnv['can'],
): PaletteRow[] {
  const q = query.trim()
  const ql = q.toLowerCase()
  if (!q) return [...recents.map(recentRow), ...FEATURED_COMMANDS.map((command) => commandRow(command, can)), askRow('')]

  const line = parseLine(q)
  if (line) return slashRows(line, can)

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
  const commands = commandsFor(q).map((command) => commandRow(command, can))
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

const ROW_LOOK: Record<RefKind, { section: Section; icon: IconName; desc: (hit: Ref) => string }> = {
  case: { section: 'Cases', icon: 'folder', desc: caseDesc },
  alert: { section: 'Alerts', icon: 'alert', desc: (hit) => joinParts([hit.data_source, hit.severity]) },
  workflow: { section: 'Agents & workflows', icon: 'flow', desc: (hit) => hit.off ?? hit.description ?? '' },
}

function choiceRows({ command }: CommandLine, target: Bound, choices: readonly Choice[]): PaletteRow[] {
  const typed = target.text.split(/\s+/)[0].toLowerCase()
  const matching = choices.filter((choice) => choice.value.startsWith(typed))
  return (matching.length > 0 ? matching : choices).map((choice) => ({
    key: `arg:${choice.value}`,
    label: choice.label,
    hint: choice.value,
    dest: 'Argument' as const,
    section: 'Close as' as const,
    icon: 'check2' as const,
    desc: choice.desc ?? '',
    disabled: false,
    fill: `${command.name} ${target.named ? `${target.id} ` : ''}${choice.value}`,
  }))
}

/** The rows under a command line that fill it in. A settled ref needs none, and a case still waiting on its text must not offer another. */
export function argumentRows(line: CommandLine, binding: Binding, lookup: RefLookup | null): PaletteRow[] {
  if (binding.state === 'off') return []
  const { command, text } = line
  const { args } = command
  const target = binding.state === 'bound' || binding.state === 'missing' ? binding.target : undefined
  if (args.kind === 'ref' && target) {
    if (args.choices) return choiceRows(line, target, args.choices)
    if (target.named || binding.state === 'missing') return []
  }
  if (!lookup) return []
  const { kind } = lookup
  const look = ROW_LOOK[kind]
  // A word that fell through to the open case is text, so it stays when another case is picked.
  const slot = args.kind === 'ref' && args.text ? ` ${target?.text ?? text}` : ''
  const seen = new Set<string>()
  return lookup.hits.flatMap((hit) => {
    if (!hit.id || seen.has(hit.id)) return []
    seen.add(hit.id)
    return [{
      key: `arg:${hit.id}`,
      label: hit.title || hit.id,
      hint: hit.id,
      dest: 'Argument' as const,
      section: look.section,
      icon: look.icon,
      desc: look.desc(hit),
      disabled: Boolean(hit.off),
      why: hit.off,
      fill: `${command.name} ${hit.id}${slot}`,
    }]
  })
}

/** The argument rows a search answer holds, the exact id first. */
function argumentHits(kind: 'case' | 'alert', hits: SearchHits): Hit[] {
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
