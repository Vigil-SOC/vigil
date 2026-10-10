import { callsPerIteration, DEFAULT_BUDGETS, type Budgets, type Entity } from "./types.js";
import { fromText, key, parseKey } from "./entities.js";
import { SpecError, type Counts, type RunSpec } from "../../core/spec.js";
import { checkpointsFrom, DEFAULT_CHECKPOINTS, type Checkpoints } from "./checkpoints.js";

// The core spec carries domain config as untyped numeric bags, so what a
// threshold means is stated here rather than in a harness the hunt shares.

export interface Verdicts {
  min_corroborating_sources: number;
  gap_lock_threshold: number;
}

// Two systems because one system agreeing with itself is not corroboration;
// three gaps because a hypothesis with that much unseen has not been cleared.
export const DEFAULT_VERDICTS: Verdicts = { min_corroborating_sources: 2, gap_lock_threshold: 3 };

export interface Termination {
  priority_floor: number;
  park_ttl_ms: number;
  hard_max_iterations: number;
  hard_max_calls: number;
  hard_max_cost_usd: number;
  hard_max_wall_ms: number;
}

// A frontier score tops out at 16, so five is a novel lead bearing on one active
// hypothesis: below it a lead is backlog rather than a reason to keep spending.
// Twice the budget, not equal to it: a ceiling on the default clamps every extension
// to what the hunt already had.
export const DEFAULT_TERMINATION: Termination = {
  priority_floor: 5,
  park_ttl_ms: 604_800_000,
  hard_max_iterations: 2 * DEFAULT_BUDGETS.max_iterations,
  hard_max_calls: 2 * DEFAULT_BUDGETS.max_calls,
  hard_max_cost_usd: 2 * DEFAULT_BUDGETS.max_cost_usd,
  hard_max_wall_ms: 2 * DEFAULT_BUDGETS.max_wall_ms,
};

export interface DigestPolicy {
  evidence_window: number;
  resurface: number;
  rare_pairing_max: number;
  graph_warmup: number;
  contrarian_max: number;
  entity_window: number;
  pivot_candidates: number;
  // The bound. evidence_window and resurface only ever add to what the lead sees,
  // and the salience floor promotes most of a long ledger past them, so without a
  // ceiling the digest grows with the hunt until it alone exceeds the request size.
  evidence_max: number;
  questions_max: number;
  omitted_ids_max: number;
}

// The bound's keys alone: stamped into a new run's spec so its ledger says which
// bound it ran under, and absent from a ledger written before there was one.
export const DIGEST_BOUNDS = {
  // The window plus its resurfaced records, with headroom for older anomalies. At a
  // typical record that renders to well under half the request ceiling.
  evidence_max: 40,
  questions_max: 20,
  // The lead expands by id, so the ids it is shown are worth more than a count, but
  // a full list on a long hunt is the same unbounded growth in a different section.
  omitted_ids_max: 50,
} as const;

export const DEFAULT_DIGEST: DigestPolicy = {
  evidence_window: 25,
  resurface: 3,
  rare_pairing_max: 1,
  graph_warmup: 20,
  contrarian_max: 3,
  entity_window: 15,
  pivot_candidates: 5,
  ...DIGEST_BOUNDS,
};

const UNBOUNDED = { evidence_max: Infinity, questions_max: Infinity, omitted_ids_max: Infinity };

// A bag the deployment left out keeps the default, key by key: a config naming
// one threshold should not silently drop the rest.
function over<T extends object>(defaults: T, held: Counts): T {
  const merged = { ...defaults } as Record<string, number>;
  for (const key of Object.keys(defaults)) {
    const value = held[key];
    if (typeof value === "number") merged[key] = value;
  }
  return merged as T;
}

// Checked against the union because verdicts and termination share one bag: a
// threshold of zero locks or proves everything, and a misspelled key is silent.
export function validateThresholds(held: Counts): void {
  const known = { ...DEFAULT_VERDICTS, ...DEFAULT_TERMINATION, max_iterations: 0 } as Record<string, number>;
  for (const [key, value] of Object.entries(held)) {
    if (!(key in known)) throw new SpecError(`unknown thresholds key: ${key}`);
    if (!Number.isFinite(value) || value <= 0) throw new SpecError(`thresholds.${key} must be a positive number`);
  }
}

export function verdictsOf(spec: RunSpec): Verdicts {
  return over(DEFAULT_VERDICTS, spec.thresholds);
}

// The ceiling rides the budget unless a config states its own, so a spec asking for
// more than the default is run rather than refused.
const atLeast = (deployment: number, asked: number): number => Math.max(deployment, asked);

export function terminationOf(spec: RunSpec): Termination {
  const budgets = budgetsOf(spec);
  return {
    ...over(DEFAULT_TERMINATION, spec.thresholds),
    // The deployment's ceiling or twice this run's ask, whichever is higher: twice the
    // ask alone makes the cap proportional to the thing it caps, locking a short run short.
    hard_max_iterations: spec.thresholds["hard_max_iterations"] ?? atLeast(DEFAULT_TERMINATION.hard_max_iterations, 2 * budgets.max_iterations),
    hard_max_calls: spec.thresholds["hard_max_calls"] ?? atLeast(DEFAULT_TERMINATION.hard_max_calls, 2 * budgets.max_calls),
    hard_max_cost_usd: spec.thresholds["hard_max_cost_usd"] ?? atLeast(DEFAULT_TERMINATION.hard_max_cost_usd, 2 * budgets.max_cost_usd),
    hard_max_wall_ms: spec.thresholds["hard_max_wall_ms"] ?? atLeast(DEFAULT_TERMINATION.hard_max_wall_ms, 2 * budgets.max_wall_ms),
  };
}

// Turns are the hunt's unit, not the harness's, whose budget block refuses added keys.
// max_calls rises with the count, or the call meter would end the hunt first.
export function budgetsOf(spec: RunSpec): Budgets {
  // A resumed run's journaled count wins over the shipped default.
  const held = (spec.budgets as Partial<Budgets>).max_iterations;
  const asked = spec.thresholds["max_iterations"] ?? held ?? DEFAULT_BUDGETS.max_iterations;
  // This run's fan-out, not the shipped one, which would cut a wider arch short.
  const perTurn = callsPerIteration(spec.dispatch.max_workers, spec.runtime.max_turns);
  return {
    ...spec.budgets,
    max_iterations: asked,
    max_calls: Math.max(spec.budgets.max_calls, asked * perTurn),
  };
}

// A ledger journaled before the digest was bounded names no evidence_max, and its
// digests were built without one. It replays under the policy that built them, the
// same way scorerOf keeps a pre-discrimination ledger on the scorer that ranked it.
export function digestOf(spec: RunSpec): DigestPolicy {
  const policy = over(DEFAULT_DIGEST, spec.digest);
  return "evidence_max" in spec.digest ? policy : { ...policy, ...UNBOUNDED };
}

export interface EnrichmentChain {
  id: string;
  on: string;
  tool: string;
  query: string;
}

export interface EnrichmentPolicy {
  max_depth: number;
  max_entities: number;
  chains: EnrichmentChain[];
}

export const DEFAULT_ENRICHMENT: EnrichmentPolicy = { max_depth: 1, max_entities: 8, chains: [] };

// The p.6 hypothesis loop: null seeded at base rate, frontier ranked by
// discrimination, termination on dominance. Per run, so a ledger replays as ranked.
export const DEFAULT_HYPOTHESIS_LOOP = false;

// Typed rather than a bag: a hypothesis is shaped by results and reshaped again,
// so it has to be a first-class record everywhere the workflow touches it.
export interface HuntSpec extends RunSpec {
  // Narrowed from the harness's: the hunt's carries the turn count too.
  budgets: Budgets;
  hypothesis_loop: boolean;
  hypotheses: string[];
  // The caller's own, kept apart from the definition's so the console can show an
  // operator that the thing they asked about is a thing being tested.
  operator_hypotheses: string[];
  // Their subjects, by statement. Parsed here so an unusable key is refused where
  // the spec is built, rather than reaching the ledger as an entity nothing means.
  operator_hypothesis_subjects: Record<string, Entity[]>;
  // The vocabulary a worker's citation is gated against, not a label per hypothesis.
  // Empty gates nothing rather than refusing everything.
  attack_techniques: string[];
  data_domains: string[];
  enrichment: EnrichmentPolicy;
  checkpoints: Checkpoints;
  termination: Termination;
}

// A ceiling under the budget it caps would clamp every extension to less than
// the hunt already had, so an operator could never buy headroom.
function validateCeilings(termination: Termination, budgets: Budgets): void {
  if (termination.hard_max_iterations < budgets.max_iterations)
    throw new SpecError(`thresholds.hard_max_iterations is below budgets.max_iterations (${budgets.max_iterations})`);
  if (termination.hard_max_calls < budgets.max_calls)
    throw new SpecError(`thresholds.hard_max_calls is below budgets.max_calls (${budgets.max_calls})`);
  if (termination.hard_max_cost_usd < budgets.max_cost_usd)
    throw new SpecError(`thresholds.hard_max_cost_usd is below budgets.max_cost_usd (${budgets.max_cost_usd})`);
  if (termination.hard_max_wall_ms < budgets.max_wall_ms)
    throw new SpecError(`thresholds.hard_max_wall_ms is below budgets.max_wall_ms (${budgets.max_wall_ms})`);
}

// Silently dropping an unparseable key would leave a Verdict about nothing and no
// trace of why. The caller is told instead: a subject they meant to name is the
// whole reason the claim can be recalled later.
function subjectsOf(held: unknown): Record<string, Entity[]> {
  if (held === undefined || held === null) return {};
  if (typeof held !== "object" || Array.isArray(held)) {
    throw new SpecError("operator_hypothesis_subjects must map a statement to its subject keys");
  }

  const parsed: Record<string, Entity[]> = {};
  for (const [statement, keys] of Object.entries(held as Record<string, unknown>)) {
    if (!Array.isArray(keys)) throw new SpecError(`the subjects of "${statement}" are not a list`);
    const subjects: Entity[] = [];
    for (const raw of keys) {
      const entity = parseKey(String(raw));
      if (entity === undefined) throw new SpecError(`${String(raw)} is not a type:value entity key`);
      if (!subjects.some((held) => held.type === entity.type && held.value === entity.value)) subjects.push(entity);
    }
    if (subjects.length > 0) parsed[statement] = subjects;
  }
  return parsed;
}

export function huntSpec(spec: RunSpec): HuntSpec {
  const held = spec.sections;
  validateThresholds(spec.thresholds);
  const budgets = budgetsOf(spec);
  validateCeilings(terminationOf(spec), budgets);
  return {
    ...spec,
    // Journaled with the run, so a replay rebuilds each digest under the same bound.
    digest: { ...DIGEST_BOUNDS, ...spec.digest },
    budgets,
    hypothesis_loop: held["hypothesis_loop"] === true,
    hypotheses: Array.isArray(held["hypotheses"]) ? (held["hypotheses"] as string[]) : [],
    operator_hypotheses: Array.isArray(held["operator_hypotheses"]) ? (held["operator_hypotheses"] as string[]) : [],
    operator_hypothesis_subjects: subjectsOf(held["operator_hypothesis_subjects"]),
    attack_techniques: Array.isArray(held["attack_techniques"]) ? (held["attack_techniques"] as string[]) : [],
    data_domains: Array.isArray(held["data_domains"]) ? (held["data_domains"] as string[]) : [],
    enrichment: { ...DEFAULT_ENRICHMENT, ...(held["enrichment"] as object | undefined) },
    checkpoints: { ...DEFAULT_CHECKPOINTS, ...checkpointsFrom(held["checkpoints"]) },
    termination: terminationOf(spec),
  };
}

// What the run's episodic read is keyed on, deduped and sorted so the same hunt
// asks the same question in the same order.
//
// Declared subjects first, and only for statements actually being put up: a
// subject keyed to a statement the caller then edited away is not what this hunt
// is about, which is the same rule core/workflows/hypothesis_subjects.py applies
// on the other side.
//
// Extraction is the fallback rather than the rule, because a declared key was
// typed by a person and parsed where an unusable one is still refused. It is not
// no fallback at all: a scheduled hunt declares no subjects -- the scheduler
// queues a hypothesis and nothing else -- so the autonomous path would otherwise
// never read memory. A key read out of a statement can be beside the point rather
// than wrong, which spends prefix budget and, because recall only reorders what to
// look at, can never move a verdict.
export function recallKeysOf(spec: HuntSpec): readonly string[] {
  const declared = spec.operator_hypotheses.flatMap((statement) => spec.operator_hypothesis_subjects[statement] ?? []);
  const named = () => [...spec.operator_hypotheses, ...spec.hypotheses].flatMap((statement) => fromText(statement));
  const held = declared.length > 0 ? declared : named();
  return [...new Set(held.map(key))].sort();
}
