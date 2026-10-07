/* The reader's stage strip: prose only. Which roles, permission rows and checkpoint
   classes a stage covers is named here by role and class; every fact about them
   (who the roles are, what they hold, which policy is set) comes from the payload. */

/** 'helpers' is all of them; a number is one helper by its place in the payload. */
export type RoleRef = 'lead' | 'helpers' | 'reviewer' | number

export interface StageDef {
  title: string
  body: string
  /** whose cards the stage shows under Who does it */
  roles: RoleRef[]
  /** whose tools it shows under What it may do on its own */
  tools: RoleRef[]
  /** also shows the handoff permission row */
  handoff?: boolean
  /** checkpoint classes that can stop here */
  checkpoints: string[]
  /** a compose stage is one helper's phase, by its place in the payload */
  helper?: number
}

export interface Strip {
  before: StageDef[]
  loop: StageDef[]
  after: StageDef[]
}

/** The payload's name for the permission row a hunt's hand off adds. */
export const HANDOFF_ROW = 'HANDOFF_IR'

export const LOOP_CAPTION = 'The loop: repeats until an explanation is proven or ruled out, or a limit is reached'

/** The four classes, in the order the reader lists them: the board's label, and the
 *  pill a stage that holds an Ask checkpoint wears. */
export const CHECKPOINT_CLASSES: Record<string, { label: string; gate: string }> = {
  hypothesis_approval: { label: 'Approve explanations before it starts', gate: 'You approve the explanations' },
  scope_extension: { label: 'Ask before widening scope', gate: 'Asks before widening scope' },
  verdict_review: { label: 'Review the verdict before closing', gate: 'You review the verdict' },
  budget_anomaly: { label: 'Ask if spending looks unusual', gate: 'Asks if spending looks unusual' },
}

const start: StageDef = {
  title: 'Start',
  body: 'Starts when someone runs it, or when something it listens for fires.',
  roles: [],
  tools: [],
  checkpoints: [],
}
const frame: StageDef = {
  title: 'Frame the case',
  body: 'Reads the case and writes the explanations to test, always including the innocent one.',
  roles: ['lead'],
  tools: [],
  checkpoints: ['hypothesis_approval'],
}
const gather: StageDef = {
  title: 'Gather evidence',
  body: 'Picks the explanation with the most to gain, then queries the sources that can confirm or rule it out.',
  roles: ['lead', 'helpers'],
  tools: ['lead', 'helpers'],
  checkpoints: ['scope_extension', 'budget_anomaly'],
}
const weigh: StageDef = {
  title: 'Weigh and review',
  body: 'Counts evidence for and against. A reviewer tries hardest to prove the innocent explanation.',
  roles: ['reviewer'],
  tools: ['reviewer'],
  checkpoints: [],
}
const decide: StageDef = {
  title: 'Decide',
  body: 'Proven, ruled out or still open: it records the verdict, and stops to ask you first if a checkpoint says so.',
  roles: ['lead'],
  tools: [],
  checkpoints: ['verdict_review'],
}
const handOff: StageDef = {
  title: 'Hand off',
  body: 'Writes the report. A proven explanation starts incident response.',
  roles: ['lead'],
  tools: [],
  handoff: true,
  checkpoints: [],
}

// keyed by run_kind; investigate and root_cause run as one agent and have no strip
const STRIPS: Record<string, Strip> = {
  hunt: { before: [start, frame], loop: [gather, weigh], after: [decide, handOff] },
  adjudicate: {
    before: [start, frame],
    loop: [gather, weigh],
    after: [decide, { ...handOff, body: 'Records a verdict and the workflow it would have run. It starts nothing.' }],
  },
}

export function stripFor(runKind: string): Strip | null {
  return STRIPS[runKind] ?? null
}

/** Compose has one stage per phase, in order; the phase's own name and agent come from the payload. */
export function composeStrip(phases: { name: string }[]): Strip {
  return {
    before: phases.map((phase, i) => ({ title: phase.name, body: '', roles: [i], tools: [i], checkpoints: [], helper: i })),
    loop: [],
    after: [],
  }
}
