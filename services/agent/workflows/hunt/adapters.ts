import { setMaxListeners } from "node:events";
import { drain, streamTurn } from "../../core/stream.js";
import type { Attempt, Harness } from "../../core/loop.js";
import { clamp } from "../../core/security.js";
import type { RoleSpec, RunSpec } from "../../core/spec.js";
import { SpecError } from "../../core/spec.js";
import { narrativeOf, renderDigest, renderDispatch, renderNullCheck } from "./render.js";
import type { HuntKinds } from "./ledger.js";
import type { DecisionProvider, DisconfirmationCritic, Narrator, WorkerDispatcher } from "./ports.js";
import { NARRATIVE_PROMPT, NARRATIVE_SCHEMA, stepsOf, type Narrative, type NarrativeAnswer } from "./narrative.js";
import type {
  Decision,
  DecisionResult,
  Digest,
  DispatchRequest,
  DispatchResult,
  NullCheckInput,
  NullCheckResult,
  RestsOn,
  ToolCall,
  WorkerEvidence,
} from "./types.js";

// The hunt's four ports over the harness. The controller decides; these only
// carry a question to a model and an answer back, and never touch the ledger.
export interface AdapterOptions {
  harness: Harness<HuntKinds>;
  spec: RunSpec;
  run_id: string;
  actions: readonly string[];
  signal?: AbortSignal;
}

function role(spec: RunSpec, name: "lead" | "critic"): RoleSpec {
  const held = spec.roles[name];
  if (held === undefined) throw new SpecError(`arch ${spec.arch} declares no ${name}, which the hunt requires`);
  return held;
}

// The lease signal and the operator's abort both end the call. Composed by hand
// rather than with AbortSignal.any, which gives no way to let go — see core/remote.ts.
interface Linked {
  signal: AbortSignal | undefined;
  release(): void;
}

// Comfortably above what one turn can attach and still low enough to warn on a leak.
const TURN_LISTENERS = 64;

export function link(held?: AbortSignal, asked?: AbortSignal): Linked {
  if (held === undefined && asked === undefined) return { signal: undefined, release: () => {} };

  // A fresh controller even to relay one source: the HTTP client attaches an abort
  // listener per request and never removes it, so a run-long signal accretes them.
  const halt = new AbortController();
  // One turn's signal serves every call that turn makes, and each attaches a listener,
  // so Node's default of ten is a false positive here. Raised, not silenced.
  setMaxListeners(TURN_LISTENERS, halt.signal);
  const sources = [held, asked].filter((one): one is AbortSignal => one !== undefined);
  const wired = sources.map((source) => {
    const relay = () => halt.abort(source.reason);
    if (source.aborted) halt.abort(source.reason);
    source.addEventListener("abort", relay, { once: true });
    return () => source.removeEventListener("abort", relay);
  });

  return { signal: halt.signal, release: () => wired.forEach((unwire) => unwire()) };
}

function turnFor(options: AdapterOptions, id: string, spec: RoleSpec, task: string, signal?: AbortSignal) {
  const held = signal ?? options.signal;
  const { runtime } = options.spec;
  return {
    run_id: options.run_id,
    run_kind: "hunt" as const,
    role: id,
    system: spec.prompt,
    task,
    schema: spec.output_schema,
    max_turns: runtime.max_turns,
    approvals: new Set(options.spec.approvals),
    verbs: options.actions,
    result_cap: runtime.result_cap,
    recall_limit: runtime.recall_limit,
    ...(held === undefined ? {} : { signal: held }),
  };
}

// What the pool moved while a call was in flight. Only correct when nothing else was
// spending, so it is for the throw path alone — a turn that returns carries its tally.
function spentOn(harness: Harness<HuntKinds>, before: number): number {
  return Math.max(0, harness.budget.spent.cost_usd - before);
}

// A call that throws has still been paid for, and the caller reads what it cost off
// error.cost_usd, which a provider error does not carry. A ceiling that cannot see
// failed work is one a failing run walks straight through.
export async function charged<T>(harness: Harness<HuntKinds>, before: number, call: Promise<T>): Promise<T> {
  try {
    return await call;
  } catch (error) {
    const spent = spentOn(harness, before);
    if (spent > 0 && typeof error === "object" && error !== null && !("cost_usd" in error)) {
      Object.defineProperty(error, "cost_usd", { value: spent, enumerable: false });
    }
    throw error;
  }
}

// One digest in, one decision out. The digest is rendered rather than handed
// over as an object, because what the lead reasons about is what it can read.
export function decisionProvider(options: AdapterOptions): DecisionProvider {
  const lead = role(options.spec, "lead");

  return {
    decide: async (digest: Digest, signal?: AbortSignal): Promise<DecisionResult> => {
      const before = options.harness.budget.spent.cost_usd;
      const linked = link(options.signal, signal);
      let outcome;
      try {
        outcome = await charged(
          options.harness,
          before,
          drain(streamTurn<Decision, HuntKinds>(turnFor(options, "lead", lead, renderDigest(digest), linked.signal), options.harness)),
        );
      } finally {
        linked.release();
      }
      if (outcome.value === null) {
        if (outcome.refusal !== null) throw new BudgetRefused(outcome.reason);
        // pending is set only when the turn parked, so the park is read off the
        // outcome rather than sniffed out of the reason text.
        if (outcome.pending !== null) throw new LeadParked(outcome.reason);
        throw new SpecError(`the lead emitted no decision: ${outcome.reason}`);
      }

      return {
        decision: outcome.value,
        model_id: options.harness.provider.model,
        prompt_version: options.spec.arch,
        cost_usd: outcome.cost_usd,
        ...(outcome.rejected.length === 0 ? {} : { rejected_attempts: outcome.rejected }),
      };
    },
  };
}

// Reuses the lead's role — same model and runtime, its own prompt and schema — since
// this is one call at the end of a run, not a participant in the loop. Billed as "narrator".
export function narrativeWriter(options: AdapterOptions): Narrator {
  const lead = role(options.spec, "lead");

  return {
    narrate: async (input: string): Promise<Narrative> => {
      const before = options.harness.budget.spent.cost_usd;
      const linked = link(options.signal);
      // No tools, and after_terminal because the account a person most wants
      // rewritten belongs to a run that has already ended.
      const spec: RoleSpec = { ...lead, prompt: NARRATIVE_PROMPT, output_schema: NARRATIVE_SCHEMA as unknown as RoleSpec["output_schema"], tools: [] };
      const turn = { ...turnFor(options, "narrator", spec, input, linked.signal), after_terminal: true };
      let outcome;
      try {
        outcome = await charged(
          options.harness,
          before,
          drain(streamTurn<NarrativeAnswer, HuntKinds>(turn, options.harness)),
        );
      } finally {
        linked.release();
      }

      // The rejected attempts are the whole of why a write-up failed: without them
      // a dead gateway and an unemittable schema read the same.
      if (outcome.value === null) {
        const why = outcome.rejected.length === 0 ? outcome.reason : `${outcome.reason}; rejected: ${outcome.rejected.join(" | ")}`;
        throw new SpecError(`the narrator did not answer: ${why}`);
      }
      return {
        summary: String(outcome.value.summary ?? ""),
        what_happened: String(outcome.value.what_happened ?? ""),
        next_steps: stepsOf(outcome.value.next_steps),
        model_id: options.harness.provider.model,
        written_at: new Date().toISOString(),
        cost_usd: outcome.cost_usd,
        ...(outcome.rejected.length === 0 ? {} : { rejected_attempts: outcome.rejected }),
      };
    },
  };
}

export interface WorkerAnswer {
  results?: unknown[];
  ips_to_check?: string[];
}

// What a worker reported, having characterised it. The strength layer counts
// corroboration and confirmation drift over exactly this provenance.
export const WORKER = "worker";

// Rows a dispatch gathered and then died before writing up. A provenance of its own,
// since no role has said what they mean and they must not read as a vouched-for finding.
export const UNSUMMARISED = "unsummarised";

// Characters of gathered output one salvage record may carry. Trimmed here because the
// digest would flatten an over-long payload to a string and cost the entities in it.
const SALVAGE_BUDGET = 6_000;

// A dispatch whose write-up failed still ran its queries, so they are kept as evidence
// with the query beside the rows. One record for the whole dispatch, not one per call.
export function salvaged(attempts: readonly Attempt[]): WorkerEvidence[] {
  const kept = attempts.filter(({ result }) => result.ok && result.rowCount > 0);
  if (kept.length === 0) return [];

  const systems = [...new Set(kept.map(({ result }) => (result.ok ? result.sourceSystem : "")))].filter((one) => one);
  let spent = 0;
  const gathered = kept.map(({ tool, args, result }) => {
    const rows = result.ok ? result.rows : [];
    const text = JSON.stringify(rows);
    // Queries are never dropped — they are what an analyst re-runs. Rows are, past the
    // budget, and the record says so rather than appearing to be all of them.
    const room = spent < SALVAGE_BUDGET;
    spent += text.length;
    return { tool, query: args, ...(room ? { rows } : { rows_dropped: rows.length }) };
  });

  return [
    {
      // Corroboration is counted over this, so naming one of several tools would credit
      // it with the others' independence.
      source_system: systems.length === 1 ? systems[0]! : "several",
      summary: `${kept.length} quer${kept.length === 1 ? "y" : "ies"} returned data that no role summarised: the dispatch failed before its write-up`,
      salience: "routine" as const,
      why_notable: "gathered before the dispatch failed, and never characterised",
      payload: { gathered },
      provenance: UNSUMMARISED,
      // Nothing has vouched for these rows, so they cannot clear a branch alone.
      attacker_influenceable: true,
      instruction_like: false,
    },
  ];
}

// Declared to the model as a JSON string: a schema saying only "object" is one a
// provider's function calling cannot shape. An object is still accepted.
function payloadOf(raw: unknown): Record<string, unknown> {
  if (typeof raw === "object" && raw !== null) return raw as Record<string, unknown>;
  if (typeof raw !== "string" || raw.trim() === "") return {};
  try {
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : { text: raw };
  } catch {
    return { text: raw };
  }
}

// The authorship vocabulary, closed: an entry naming anything else is dropped rather
// than trusted, because sensorAttested() reads this to decide whether a claim can carry
// a verdict, and an unknown value must never read as an attestation.
const AUTHORSHIP: ReadonlySet<string> = new Set(["sensor", "adversary", "third_party"]);

// What a finding rests on, field by field. Entries the schema let through but that name
// no field, or an authorship this side does not know, are dropped: a record left with
// none falls back to the record-level boolean.
function restsOn(raw: unknown): RestsOn[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((entry) => {
    const basis = entry as Record<string, unknown>;
    const field = typeof basis["field"] === "string" ? basis["field"].trim() : "";
    const authored = basis["authored"];
    if (field === "" || typeof authored !== "string" || !AUTHORSHIP.has(authored)) return [];
    return [{ field, authored: authored as RestsOn["authored"] }];
  });
}

// A worker's emission, as evidence records. Anything the schema did not require is
// dropped rather than guessed at: the controller stamps identity and time.
export function evidenceFrom(answer: WorkerAnswer): WorkerEvidence[] {
  if (!Array.isArray(answer.results)) return [];
  return answer.results.map((row) => {
    const record = row as Record<string, unknown>;
    return {
      // Stamped here or nowhere: the drift guard filters on it, and an unset
      // provenance switches the guard off silently rather than failing.
      provenance: WORKER,
      instruction_like: false,
      attacker_influenceable: false,
      source_system: String(record["source_system"] ?? ""),
      summary: String(record["summary"] ?? ""),
      salience: (record["salience"] ?? "routine") as WorkerEvidence["salience"],
      why_notable: String(record["why_notable"] ?? ""),
      payload: payloadOf(record["payload"]),
      ...(Array.isArray(record["supports"]) ? { supports: record["supports"] as string[] } : {}),
      ...(Array.isArray(record["weakens"]) ? { weakens: record["weakens"] as string[] } : {}),
      ...(typeof record["attacker_influenceable"] === "boolean"
        ? { attacker_influenceable: record["attacker_influenceable"] }
        : {}),
      ...(restsOn(record["rests_on"]).length > 0 ? { rests_on: restsOn(record["rests_on"]) } : {}),
      ...(typeof record["attack_technique"] === "string" && record["attack_technique"] !== ""
        ? { attack_technique: record["attack_technique"] }
        : {}),
    };
  });
}

// The pool refused another call, which is nothing like a lead that emitted badly:
// the run spent what it was given and is over. Its own error because the hunt loop
// ends on it, the way compose, lead and tally all end on outcome.refusal.
export class BudgetRefused extends Error {}

// The lead stopped on a checkpoint nobody has answered. Its own error for the same
// reason BudgetRefused is: a bounded re-ask cannot change it, and dies pretending it can.
export class LeadParked extends Error {}

// A failure is a result, not a throw: a worker that burned tokens and then died
// still spent them, and the controller records the gap either way.
export function workerDispatcher(options: AdapterOptions): WorkerDispatcher {
  return {
    dispatch: async (request: DispatchRequest): Promise<DispatchResult> => {
      const worker = options.spec.roles.workers[request.agent_id];
      if (worker === undefined) {
        return {
          dispatch_id: request.dispatch_id,
          evidence: [],
          failed: true,
          failure_reason: `no worker ${request.agent_id} in this arch`,
          cost_usd: 0,
        };
      }

      const before = options.harness.budget.spent.cost_usd;
      // The dispatch's own signal wins where it has one: an operator halting the
      // hunt mid-query is a narrower stop than the run losing its lease.
      const scoped = { ...options, ...(request.signal === undefined ? {} : { signal: request.signal }) };
      const task = renderDispatch(request, narrativeOf(options.spec));
      const linked = link(options.signal, request.signal);
      let outcome;
      try {
        outcome = await charged(
          options.harness,
          before,
          drain(
            streamTurn<WorkerAnswer, HuntKinds>(
              turnFor(scoped, request.agent_id, worker, task, linked.signal),
              options.harness,
            ),
          ),
        );
      } finally {
        linked.release();
      }

      const cost_usd = outcome.cost_usd;
      if (outcome.value === null) {
        return {
          dispatch_id: request.dispatch_id,
          evidence: salvaged(outcome.calls),
          calls: callsOf(outcome.calls),
          failed: true,
          failure_reason: outcome.reason,
          cost_usd,
        };
      }
      const questions = Array.isArray(outcome.value.ips_to_check) ? outcome.value.ips_to_check : [];
      return {
        dispatch_id: request.dispatch_id,
        evidence: evidenceFrom(outcome.value),
        calls: callsOf(outcome.calls),
        ...(questions.length === 0 ? {} : { questions }),
        failed: false,
        failure_reason: "",
        cost_usd,
      };
    },
  };
}

// Total characters of tool output one dispatch may journal. Shared rather than
// per-call: one 500-row answer must not crowd the record of the calls after it.
const CALL_BUDGET = 16_000;

// The execution log the audit trail needs. wrapped.text is what the worker was
// actually shown -- already scrubbed, delimiter-safe and capped at result_cap by
// wrap() -- so journaling it cannot drift from what the model read. Arguments are
// the query itself, so they are what an analyst re-runs and are never dropped.
export function callsOf(attempts: readonly Attempt[]): ToolCall[] {
  if (attempts.length === 0) return [];
  const share = Math.max(1, Math.floor(CALL_BUDGET / attempts.length));
  return attempts.map(({ tool, args, wrapped }) => ({
    tool,
    arguments: clamp(args, share),
    result: clamp(wrapped.text, share),
  }));
}

interface CriticAnswer {
  benign_explanation?: string;
  benign_explanation_stands?: boolean;
  rationale?: string;
}

// The critic argues the benign case against the raw evidence. Its answer is
// inverted here: the hypothesis survives exactly when the benign case does not.
export function disconfirmationCritic(options: AdapterOptions): DisconfirmationCritic {
  const critic = role(options.spec, "critic");

  return {
    argueNull: async (check: NullCheckInput): Promise<NullCheckResult> => {
      const before = options.harness.budget.spent.cost_usd;
      const task = renderNullCheck(check);
      const linked = link(options.signal);
      let outcome;
      try {
        outcome = await charged(
          options.harness,
          before,
          drain(
            streamTurn<CriticAnswer, HuntKinds>(turnFor(options, "critic", critic, task, linked.signal), options.harness),
          ),
        );
      } finally {
        linked.release();
      }

      const cost_usd = outcome.cost_usd;
      // A critic that could not answer leaves the hypothesis standing rather than
      // proving it: an unargued null is not a null that failed.
      if (outcome.value === null) {
        return {
          survives: true,
          strongest_benign_explanation: "",
          rationale: `the critic did not answer: ${outcome.reason}`,
          cost_usd,
          model_id: options.harness.provider.model,
          prompt_version: options.spec.arch,
        };
      }

      return {
        survives: outcome.value.benign_explanation_stands !== true,
        strongest_benign_explanation: String(outcome.value.benign_explanation ?? ""),
        rationale: String(outcome.value.rationale ?? ""),
        cost_usd,
        model_id: options.harness.provider.model,
        prompt_version: options.spec.arch,
      };
    },
  };
}
