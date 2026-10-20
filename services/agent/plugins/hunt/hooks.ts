import type { CallPayload, NewEvent, TerminalHandoff } from "../../contracts/events.js";
import { forPeople, guardedRef, guardsOn, readOf, runValues, typeSafe, valuesBehind, type RunValues } from "../../core/guards.js";
import { recalledPayloadOf } from "../../contracts/memory.js";
import { isStructured } from "../../contracts/plugin.js";
import { defineTool, type RegisteredTool, type ToolResult } from "../../contracts/tool.js";
import { agentCalls, callsIn, childOrdinal, runChild, SKILL_TOOL, type Early, type Ending, type Parked, type PluginContext, type PluginHooks } from "../../core/agent.js";
import { classOf, okWith, RECORD_TOOLS, refusedWith, type RecordBackend } from "../../core/record.js";
import type { Harness } from "../../core/loop.js";
import { errorFields, logger } from "../../core/log.js";
import type { State } from "../../core/seams.js";
import { narrowFinding, type RunSpec, type ToolSpec } from "../../core/spec.js";
import { isVisibilityGap } from "../../core/security.js";
import { toolsFrom } from "../../tools/remote.js";
import { disconfirmationCritic, evidenceFrom, narrativeWriter, type AdapterOptions } from "./adapters.js";
import { pendingCheckpoints } from "./checkpoints.js";
import { suppressedEntities } from "./frontier.js";
import { allowanceOf, huntSpec } from "./config.js";
import { expandFrom } from "./expand.js";
import { citedFindings } from "./findings.js";
import { Journal, type HuntKinds } from "./journal.js";
import type { HuntEvent, Projection } from "./ledger.js";
import { narrativeInput, type Narrative } from "./narrative.js";
import { startHunt } from "./opening.js";
import type { DirectiveQueue } from "./ports.js";
import { buildReport, renderReport } from "./report.js";
import { operatorLine } from "./render.js";
import { sensorAttested } from "./strength.js";
import type { Handoff, HuntOutcome } from "./types.js";
import { HuntBook } from "./book.js";

const log = logger("agent.hunt");

// The hunt plugin's code: the record's tools, and hooks that open it, apply what operators
// say and end it. Loop, parking and stopping are the kernel's.

export interface HuntPorts {
  queue: DirectiveQueue;
  // Only a forward hunt escalates: an adjudication proposes and executes nothing.
  handoffs: boolean;
  onHandoff?: (runId: string, handoff: TerminalHandoff) => Promise<boolean>;
  onFindings?: (runId: string, findingIds: readonly string[]) => Promise<boolean>;
  onCost?: (runId: string, costUsd: number) => Promise<void>;
  // The iteration the hunt has reached and the ceiling it runs under, for the case
  // list (#2209). Fail-open on its own: a push that did not land is overwritten by the next.
  onProgress?: (runId: string, iteration: number, maxCostUsd: number) => Promise<void>;
}

// The tools this plugin owns. Everything else the agent calls is the deployment's.
export const HUNT_TOOLS = [
  "hunt_status",
  "record_evidence",
  "validate_hypothesis",
  "abandon_hypothesis",
  "handoff_ir",
  "close_question",
  "ask_operator",
  "conclude_hunt",
] as const;

const OWN = new Set<string>([...HUNT_TOOLS, ...RECORD_TOOLS, "expand"]);

function state(ctx: PluginContext): State<HuntKinds> {
  return ctx.harness.state as unknown as State<HuntKinds>;
}

// The hunt's record, read fresh off the ledger and written back in one flush. The
// projection a tool reads is the stored one, so two tools never disagree.
async function withBook<T>(ctx: PluginContext, ports: HuntPorts, use: (book: HuntBook) => T | Promise<T>): Promise<T> {
  const ledger = await Journal.open(state(ctx), ports.queue, ctx.options.run_id, ctx.options.run_kind);
  const book = new HuntBook(ledger, ctx.harness.budget);
  const value = await use(book);
  await ledger.flush();
  return value;
}

function adapters(ctx: PluginContext): AdapterOptions {
  return {
    harness: ctx.harness as unknown as Harness<HuntKinds>,
    spec: ctx.options.spec,
    run_id: ctx.options.run_id,
    run_kind: ctx.options.run_kind,
    actions: [],
    recall_keys: [],
    ...(ctx.options.signal === undefined ? {} : { signal: ctx.options.signal }),
    // The critic and the narrator are judges, each run on a ledger of its own.
    turn: async (cfg) => runChild(ctx, `${cfg.role}:${await childOrdinal(ctx, cfg.role)}`, cfg),
  };
}

// The kernel's record tools on the hunt's record: a proposal joins the board, a link is a
// ruling on an evidence id record_evidence returned, a question joins the frontier.
function huntRecord(ctx: PluginContext, ports: HuntPorts): RecordBackend {
  return {
    stances: ["supports", "weakens", "neither"],
    evidence: "an evidence id record_evidence returned",
    propose: async (statement) => withBook(ctx, ports, (book) => okWith([{ hypothesis_id: book.propose(statement) }])),
    link: async (links) =>
      withBook(ctx, ports, (book) => {
        const ruled = book.rule(links);
        return ruled.ruled === 0 && ruled.skipped.length > 0 ? refusedWith(ruled.skipped.join("; ")) : okWith([ruled]);
      }),
    ask: async (question, hypothesisId) =>
      withBook(ctx, ports, (book) => {
        if (hypothesisId !== null && !book.projection.hypotheses.has(hypothesisId)) return refusedWith(`${hypothesisId} is not on the board`);
        return okWith([{ question_id: book.raise(question, { hypothesis_id: hypothesisId }) }]);
      }),
  };
}

// The agent's turn budget is the iterations asked for times the allowance per
// iteration; a pinned hard ceiling scales the same way.
export function kernelSpec(spec: RunSpec): RunSpec {
  const asked = huntSpec(spec).budgets.max_iterations;
  const factor = allowanceOf(spec).turns_per_iteration;
  const pinned = spec.thresholds["hard_max_iterations"];
  return {
    ...spec,
    thresholds: {
      ...spec.thresholds,
      max_iterations: asked * factor,
      ...(pinned === undefined ? {} : { hard_max_iterations: pinned * factor }),
    },
  };
}

export function huntHooks(ports: HuntPorts): PluginHooks {
  const filed = new Set<string>();
  const linked = new Set<string>();

  return {
    open: async (ctx, base) => {
      await startHunt(state(ctx), ports.queue, ctx.options.run_id, huntSpec(kernelSpec(base.spec as RunSpec)), base.started_by, ctx.options.run_kind, ctx.options.plugin.needs);
    },

    tools: (ctx) => [...huntTools(ctx, ports), ...expandTool(ctx)],

    record: (ctx) => huntRecord(ctx, ports),

    turns: async (ctx) => withBook(ctx, ports, (book) => book.hunt.budgets.max_iterations),

    before: async (ctx): Promise<Early | null> =>
      withBook(ctx, ports, async (book) => {
        // The pool was built from the spec this run started on, so a resumed hunt would
        // be refused on the ceiling an extension already lifted.
        const granted = book.hunt.budgets;
        ctx.harness.budget.raise({ max_cost_usd: granted.max_cost_usd, max_wall_ms: granted.max_wall_ms });
        await book.applyDirectives();
        book.reconcile();
        if (book.ended()) {
          await book.ledger.flush();
          return { kind: "end", ...(await ending(ctx, book)) };
        }
        const { status } = book.hunt;
        if (status === "parked" || status === "pending_approval") {
          const [open] = pendingCheckpoints(book.projection);
          // The park's own reason where it has one: the console shows it on the run row.
          const reason = status === "parked" && book.hunt.parked_reason ? book.hunt.parked_reason : book.suspendedBecause();
          return { kind: "park", reason, ...(open === undefined ? {} : { checkpoint_id: open.checkpoint_id }) };
        }
        return null;
      }),

    brief: async (ctx) => withBook(ctx, ports, (book) => briefOf(book.projection)),

    observe: async (ctx, seen): Promise<void | "halt" | Parked> => {
      if (seen.type !== "tool_result") return;
      const turn = callsIn(await state(ctx).read(ctx.options.run_id)).reduce((max, call) => Math.max(max, call.turn), 0);
      const settled = await withBook(ctx, ports, (book) => {
        book.advanceTo(turn);
        // A call the estate could not answer is a blind spot the verdicts count.
        const failure = seen.attempt.wrapped.failure;
        if (!OWN.has(seen.call.tool) && failure !== null && isVisibilityGap(failure)) {
          book.appendGap(seen.call.tool, failure.kind as "timeout" | "unavailable" | "backend_error", null);
        }
        return {
          projection: book.projection,
          ended: book.ended(),
          concluded: book.hunt.outcome === "completed" || book.hunt.outcome === "data_starved",
          suspended: book.hunt.status === "parked" || book.hunt.status === "pending_approval",
          why: book.suspendedBecause(),
        };
      });
      await reportOut(ctx, ports, settled.projection, filed, linked);
      // A hunt that concluded on its own predicate (data_starved is an honest inconclusive)
      // is asked for its answer, the disposition among it, before it ends; any other
      // ending, and any call after the conclusion, halts at once.
      const answers = settled.concluded && seen.call.tool === "conclude_hunt" && seen.attempt.result.ok && asksDisposition(ctx);
      if (settled.ended && !answers) return "halt";
      if (settled.suspended) {
        const [open] = pendingCheckpoints(settled.projection);
        return { parked: settled.why, ...(open === undefined ? {} : { checkpoint_id: open.checkpoint_id }) };
      }
    },

    halted: async (ctx): Promise<Ending | Parked> => withBook(ctx, ports, (book) => ending(ctx, book)),

    // Experimental (VIGIL_GUARDS=on): an agent that stops with a hypothesis
    // still active is told so once, with what it can do, rather than ending the hunt
    // inconclusive on a board it had not finished.
    unsettled: async (ctx): Promise<string | null> => {
      if (!guarded(ctx)) return null;
      return withBook(ctx, ports, (book) => {
        const open = [...book.projection.hypotheses.values()].filter((one) => one.status === "active");
        if (open.length === 0 || book.ended()) return null;
        const list = open.map((one) => `${one.hypothesis_id} (${one.statement})`).join("; ");
        return `The hunt is not finished: ${list} ${open.length === 1 ? "is" : "are"} still active. For each, do one of: put it up for a verdict with validate_hypothesis if new evidence bears on it; abandon it with abandon_hypothesis citing the evidence that rules it out; or run the queries that would settle it. Then conclude_hunt.`;
      });
    },

    // The agent stopped calling tools. A hunt ends on its own predicate, not on the
    // agent's say-so: at a ceiling it concludes if it can and parks if it cannot.
    finish: async (ctx, outcome): Promise<Ending | Parked> =>
      withBook(ctx, ports, async (book) => {
        // Concluded by its own tool, then answered: nothing left to decide.
        if (book.ended()) {
          await book.ledger.flush();
          return ending(ctx, book);
        }
        // The harness refused a call: usually the wall clock, which stops a hunt with
        // turns and dollars still on the board. It parks on extend, conclude or abort.
        if (outcome.refusal !== null) {
          const why = book.parkOnRefusal(outcome.reason);
          await book.ledger.flush();
          return { parked: why };
        }
        // Its own turns ran out: concluded if the board allows it, parked if not.
        if (outcome.capped) {
          if (book.atCeiling("the hunt used its turns") === "parked") {
            await book.ledger.flush();
            return { parked: book.hunt.parked_reason ?? book.suspendedBecause() };
          }
        } else if (outcome.status !== "completed") {
          book.terminate("failed", outcome.reason);
        } else if (!book.ended()) {
          const said = summaryOfAnswer(outcome.value);
          const concluded = book.conclude(said === "" ? "the agent stopped" : `the agent stopped: ${said}`);
          if (concluded.parked !== undefined) {
            await book.ledger.flush();
            return { parked: book.suspendedBecause(), checkpoint_id: concluded.parked };
          }
          if (!concluded.concluded) book.terminate("completed", `the agent stopped before the hunt could conclude (${concluded.said})${said === "" ? "" : `: ${said}`}`);
        }
        await book.ledger.flush();
        return ending(ctx, book);
      }),

    // The gateway's own allowance is not the hunt failing: it ends on the budget,
    // with its report, like any other ceiling it could not lift.
    exhausted: async (ctx, reason) =>
      withBook(ctx, ports, async (book) => {
        if (book.ended()) {
          await book.ledger.flush();
          return ending(ctx, book);
        }
        book.terminate("budget_terminated", reason);
        await book.ledger.flush();
        return { ...(await ending(ctx, book)), outcome: "budget_exhausted" as const, reason };
      }),

    stopped: async (ctx) =>
      withBook(ctx, ports, async (book) => {
        book.terminate("aborted", "an operator halted the hunt");
        await book.ledger.flush();
        return (await ending(ctx, book)).summary ?? undefined;
      }),
  };
}

// What a finished hunt leaves: the report, the account of it, and its escalations.
async function ending(ctx: PluginContext, book: HuntBook): Promise<Ending> {
  await book.ledger.flush();
  const projection = book.projection;
  const built = buildReport(projection);
  const narrative = await narrate(ctx, projection, built);
  const events = (await state(ctx).read(ctx.options.run_id)) as readonly HuntEvent[];
  // A person reads the report and the handoffs: handles become their values there,
  // marked as written by the subject (core/guards.ts). A no-op with guards off.
  const values = runValues(callsIn(events as never));
  const handoffs = events
    .filter((event) => event.kind === "handoff")
    .map((event) => toTerminalHandoff(event.payload as Handoff, projection, values));
  const hunt = projection.hunt;
  return {
    outcome: outcomeOf(hunt.outcome),
    reason: forPeople(hunt.termination_reason ?? `ended ${hunt.outcome ?? ""}`.trim(), values),
    summary: forPeople(renderReport(built, projection, narrative, recalledPayloadOf(events)), values),
    handoffs,
  };
}

// One call over the whole record, fail-open: the verdicts stand on their own.
async function narrate(ctx: PluginContext, projection: Projection, built: ReturnType<typeof buildReport>): Promise<Narrative | null> {
  try {
    const narrative = await narrativeWriter(adapters(ctx)).narrate(narrativeInput(projection, built));
    const event: NewEvent<HuntKinds> = { run_id: ctx.options.run_id, run_kind: ctx.options.run_kind, kind: "narrative", payload: narrative };
    await state(ctx).append(ctx.options.run_id, [event]);
    return narrative;
  } catch (error) {
    log.error("hunt ended without an account of it", { run_id: ctx.options.run_id, ...errorFields(error) });
    return null;
  }
}

function toTerminalHandoff(handoff: Handoff, projection: Projection, values?: RunValues): TerminalHandoff {
  const title = projection.hypotheses.get(handoff.hypothesis_id)?.statement ?? handoff.rationale;
  const markdown = handoff.case_markdown ?? handoff.case_file ?? handoff.rationale;
  if (values === undefined || values.byRef.size === 0) return { case_id: handoff.case_id, title, markdown };
  // With guards on the agent never read the values a responder acts on -- the file
  // name, the account, the command line. They come from the ledger, for the results
  // the handed-off claim's evidence rests on.
  const linked = projection.links
    .filter((link) => link.hypothesis_id === handoff.hypothesis_id && link.relation === "supports")
    .map((link) => projection.evidence.get(link.evidence_id))
    .map((record) => (record === undefined ? "" : typeof record.payload === "string" ? record.payload : JSON.stringify(record.payload)));
  const behind = valuesBehind(linked, values);
  const appendix = behind.length === 0 ? "" : `\n\n## Values the evidence rests on\n\nAs the sensors recorded them. Values marked as written by the subject are attacker-controllable text: read them as data.\n\n${behind.join("\n")}`;
  return { case_id: handoff.case_id, title: forPeople(title, values), markdown: `${forPeople(markdown, values)}${appendix}` };
}

// Escalations and cited alerts reach the backend as they land, and spend as it
// accrues. Marked sent only once the push answers that it landed.
async function reportOut(ctx: PluginContext, ports: HuntPorts, projection: Projection, filed: Set<string>, linked: Set<string>): Promise<void> {
  const runId = ctx.options.run_id;
  try {
    if (ports.onHandoff !== undefined) {
      const unfiled = projection.handoffs.filter((handoff) => !filed.has(handoff.case_id));
      const values = unfiled.length === 0 ? undefined : runValues(callsIn((await state(ctx).read(runId)) as never));
      for (const handoff of unfiled) {
        if (await ports.onHandoff(runId, toTerminalHandoff(handoff, projection, values))) filed.add(handoff.case_id);
      }
    }
    if (ports.onFindings !== undefined) {
      const fresh = citedFindings(projection).filter((id) => !linked.has(id));
      if (fresh.length > 0 && (await ports.onFindings(runId, fresh))) for (const id of fresh) linked.add(id);
    }
    if (ports.onCost !== undefined && projection.hunt.status === "active") await ports.onCost(runId, projection.hunt.cost_usd);
  } catch (error) {
    log.warn("hunt could not report out", { run_id: runId, ...errorFields(error) });
  }
  if (ports.onProgress === undefined) return;
  try {
    await ports.onProgress(runId, projection.hunt.iteration, projection.hunt.budgets.max_cost_usd);
  } catch (error) {
    log.warn("hunt could not report its progress", { run_id: runId, ...errorFields(error) });
  }
}

// The hunt's own outcomes, as the ledger's. inconclusive is a completed run that
// reported honestly, not a failure.
function outcomeOf(outcome: HuntOutcome | null): Ending["outcome"] {
  if (outcome === "aborted") return "aborted";
  if (outcome === "budget_terminated") return "budget_exhausted";
  if (outcome === "failed") return "failed";
  return "completed";
}

// The board: every hypothesis with its id and standing, and the open questions. Rendered
// at each pick-up, so the agent works from the record rather than its memory of it.
export function briefOf(projection: Projection): string {
  const hunt = projection.hunt;
  const operator = operatorOf(projection);
  const hypotheses = [...projection.hypotheses.values()].map(
    (one) => `- ${one.hypothesis_id} [${one.status}${one.provenance === "base_rate" ? ", the benign explanation" : ""}] ${one.statement}`,
  );
  const questions = [...projection.questions.values()].filter((one) => one.status === "open").map((one) => `- ${one.question_id} ${one.question}`);
  const domains = hunt.spec.data_domains.length === 0 ? "" : `Declared data domains (name one as source_system): ${hunt.spec.data_domains.join(", ")}`;
  return [
    `Hunt: ${hunt.name}`,
    hunt.narrative,
    `## Hypotheses\n${hypotheses.join("\n") || "- none"}`,
    questions.length === 0 ? "" : `## Open questions\n${questions.join("\n")}`,
    domains,
    operator.length === 0 ? "" : `## From the operator and the hunt's own rules\n${operator.join("\n")}`,
    `Budget: turn ${hunt.iteration} of ${hunt.budgets.max_iterations}, $${hunt.cost_usd.toFixed(4)} of $${hunt.budgets.max_cost_usd.toFixed(2)}.`,
    "Call hunt_status whenever you need the board again.",
  ]
    .filter((part) => part)
    .join("\n\n");
}

// What an operator said and the hunt's rules said back (a rejection, a cleared entity, a
// corrected premise), as the lead read them off its digest. Newest last, bounded.
const OPERATOR_SHOWN = 12;

export function operatorOf(projection: Projection): string[] {
  // Every operator-supplied string is scrubbed, capped and kept to one line: the
  // brief is what the agent works from, and a forged heading would read as its own.
  const benign = [...suppressedEntities(projection).entries()].map(
    ([entity, why]) => `- ${operatorLine(entity)} is known-benign${why ? ` (${operatorLine(why)})` : ""}: its evidence stands; open no new work on it.`,
  );
  const said = projection.directives
    .filter((directive) => directive.kind !== "benign" && typeof directive.text === "string" && directive.text.trim() !== "")
    .slice(-OPERATOR_SHOWN)
    .map((directive) => `- ${directive.kind === "reject" ? "rejected" : directive.kind} (${operatorLine(directive.actor)}): ${operatorLine(directive.text.trim())}`);
  return [...benign, ...said];
}

function statusOf(projection: Projection): Record<string, unknown> {
  return {
    operator: operatorOf(projection),
    hunt: { status: projection.hunt.status, turn: projection.hunt.iteration, budgets: projection.hunt.budgets, cost_usd: projection.hunt.cost_usd },
    hypotheses: [...projection.hypotheses.values()].map((one) => ({
      hypothesis_id: one.hypothesis_id,
      statement: one.statement,
      status: one.status,
      provenance: one.provenance,
      resolution_reason: one.resolution_reason,
      linked: projection.links.filter((link) => link.hypothesis_id === one.hypothesis_id).length,
    })),
    evidence: [...projection.evidence.values()].slice(-40).map((one) => ({ evidence_id: one.evidence_id, source_system: one.source_system, summary: one.summary, salience: one.salience })),
    questions: [...projection.questions.values()].filter((one) => one.status === "open"),
  };
}

// --- tools ---------------------------------------------------------------------

// plugin.yaml's finding, for a ledger journaled before the spec carried one.
const RESULT_ITEM_FALLBACK = {
  type: "object",
  additionalProperties: false,
  required: ["source_system", "summary", "salience", "why_notable", "payload"],
  properties: {
    source_system: { type: "string" },
    summary: { type: "string" },
    salience: { type: "string", enum: ["routine", "notable", "anomalous"] },
    why_notable: { type: "string" },
    payload: { type: "string" },
    supports: { type: "array", items: { type: "string" } },
    weakens: { type: "array", items: { type: "string" } },
    query_failed: { type: "boolean", description: "True when the query behind this row failed, timed out or returned nothing." },
    attacker_influenceable: { type: "boolean" },
    rests_on: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["field", "authored"],
        properties: { field: { type: "string" }, authored: { type: "string", enum: ["sensor", "adversary", "third_party"] } },
      },
    },
    attack_technique: { type: "string" },
  },
};

// The plugin's finding shape, narrowed at spec build to the playbook's data domains and
// techniques. A ledger journaled before the spec carried one narrows the fallback here.
function resultItem(spec: RunSpec): Record<string, unknown> {
  return spec.finding ?? narrowFinding(RESULT_ITEM_FALLBACK, spec.sections);
}

// What the agent said when it stopped: its summary, or prose from a ledger
// written before the hunt concluded in a shape.
function summaryOfAnswer(value: unknown): string {
  if (typeof value === "string") return value.trim();
  const summary = (value as { summary?: unknown } | null)?.summary;
  return typeof summary === "string" ? summary.trim() : "";
}

function ok(rows: readonly unknown[]): ToolResult {
  return { ok: true, rows, rowCount: rows.length, capped: false, sourceSystem: "hunt" };
}

function refused(detail: string): ToolResult {
  return { ok: false, failure: { kind: "refused", detail } };
}

function local(id: string, description: string, parameters: Record<string, unknown>, execute: (args: Record<string, unknown>) => Promise<ToolResult>): RegisteredTool {
  return defineTool({ id, description, parameters, execute }, { maxRows: 200, timeoutMs: 600_000 }, true);
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((one): one is string => typeof one === "string") : [];
}

function huntTools(ctx: PluginContext, ports: HuntPorts): RegisteredTool[] {
  const tools: RegisteredTool[] = [
    local("hunt_status", "The hunt's board: hypotheses with ids and standing, recent evidence ids, open questions and budget.", { type: "object", additionalProperties: false, properties: {} }, async () =>
      withBook(ctx, ports, (book) => ok([statusOf(book.projection)])),
    ),

    local(
      "record_evidence",
      "Record findings from what your queries returned, one per telemetry domain. Name hypotheses each finding supports or weakens, and what it rests on. Returns the evidence ids.",
      {
        type: "object",
        additionalProperties: false,
        required: ["results"],
        properties: {
          results: { type: "array", items: guarded(ctx) ? withRef(resultItem(ctx.options.spec)) : resultItem(ctx.options.spec) },
          follow_up: {
            type: "array",
            description: "New leads this opened, each a question worth asking next.",
            items: { type: "object", additionalProperties: false, required: ["question"], properties: { question: { type: "string" }, entity: { type: "string" }, hypothesis_id: { type: "string" } } },
          },
        },
      },
      async (args) => {
        const given = Array.isArray(args["results"]) ? (args["results"] as Record<string, unknown>[]) : [];
        const results = guarded(ctx) ? await fromRefs(ctx, given) : given;
        const calls = callsIn(await state(ctx).read(ctx.options.run_id));
        const served = servedSince(calls, ctx.options.spec.tools);
        const call_refs = servedRefsSince(calls, ctx.turn());
        return withBook(ctx, ports, (book) => {
          const evidence = evidenceFrom({ results }).map((result) => ({ ...result, call_refs, ...(served === null ? {} : { served_by: served }) }));
          const records = book.appendEvidence(evidence);
          const raised = (Array.isArray(args["follow_up"]) ? args["follow_up"] : []).map((one) => {
            const lead = one as { question?: unknown; entity?: unknown; hypothesis_id?: unknown };
            return book.raise(String(lead.question ?? ""), {
              entity_key: typeof lead.entity === "string" ? lead.entity : null,
              spawning_evidence_id: records[0]?.evidence_id ?? null,
              hypothesis_id: typeof lead.hypothesis_id === "string" && book.projection.hypotheses.has(lead.hypothesis_id) ? lead.hypothesis_id : null,
            });
          });
          return ok([{ evidence_ids: records.map((one) => one.evidence_id), questions: raised }]);
        });
      },
    ),

    local(
      "validate_hypothesis",
      "Put one hypothesis up for a verdict. An independent critic argues the strongest benign explanation; it reaches proven only by surviving that, on corroboration from more than one domain.",
      { type: "object", additionalProperties: false, required: ["hypothesis_id"], properties: { hypothesis_id: { type: "string" } } },
      async (args) => {
        const hypothesisId = String(args["hypothesis_id"] ?? "");
        const input = await withBook(ctx, ports, (book) => {
          const hypothesis = book.projection.hypotheses.get(hypothesisId);
          if (hypothesis === undefined) return `${hypothesisId} is not on the ledger`;
          if (hypothesis.status !== "active") return `${hypothesisId} is already ${hypothesis.status}; no second verdict was run`;
          if (guarded(ctx)) {
            const again = unchangedSinceCritic(book.projection, hypothesisId);
            if (again !== null) return again;
          }
          return book.nullCheckInput(hypothesis);
        });
        if (typeof input === "string") return refused(input);
        let result;
        try {
          result = await disconfirmationCritic(adapters(ctx)).argueNull(input);
        } catch (error) {
          // A critic that cannot run fails closed: an unavailable argument is not a won one.
          return refused(`the disconfirmation critic failed (${(error as Error).message}), so ${hypothesisId} stays active`);
        }
        const argued = input.evidence.map((linked) => linked.record.evidence_id);
        return withBook(ctx, ports, (book) => ok([book.applyVerdict(hypothesisId, result, argued)]));
      },
    ),

    local(
      "abandon_hypothesis",
      "Drop a hypothesis branch. Refused unless at least one cited record rests on something the telemetry attested rather than the adversary authored.",
      {
        type: "object",
        additionalProperties: false,
        required: ["hypothesis_id", "reason", "evidence_ids"],
        properties: { hypothesis_id: { type: "string" }, reason: { type: "string" }, evidence_ids: { type: "array", items: { type: "string" } } },
      },
      async (args) =>
        withBook(ctx, ports, (book) => {
          const cited = strings(args["evidence_ids"]);
          const attested = cited.map((id) => book.projection.evidence.get(id)).some((record) => record !== undefined && sensorAttested(record));
          if (!attested) return refused("no cited record rests on something the telemetry attested; corroborate before you stop looking");
          return ok([{ said: book.abandon(String(args["hypothesis_id"] ?? ""), String(args["reason"] ?? ""), cited) }]);
        }),
    ),

    local(
      "close_question",
      "Close an open question you have answered or ruled out, and say why.",
      { type: "object", additionalProperties: false, required: ["question_id", "reason"], properties: { question_id: { type: "string" }, reason: { type: "string" } } },
      async (args) =>
        withBook(ctx, ports, (book) => {
          const refusal = book.close(String(args["question_id"] ?? ""), String(args["reason"] ?? ""));
          return refusal === null ? ok([{ closed: args["question_id"] }]) : refused(refusal);
        }),
    ),

    local(
      "ask_operator",
      "Stop and ask the operator when something is wrong that no query will fix: the evidence contradicts the hunt's premise, you are covering the same ground, or what you found is outside what this hunt was set up to answer.",
      { type: "object", additionalProperties: false, required: ["question"], properties: { question: { type: "string" }, hypothesis_id: { type: "string" } } },
      async (args) =>
        withBook(ctx, ports, (book) => ok([book.askOperator(String(args["question"] ?? ""), typeof args["hypothesis_id"] === "string" ? args["hypothesis_id"] : null)])),
    ),

    local(
      "conclude_hunt",
      ports.handoffs
        ? "Recommend ending the hunt. Refused while a hypothesis is still active and could be moved, or an open question is still worth asking."
        : "Conclude the adjudication. Name the catalogue workflow you would have run (or none) in proposed_workflow, and say in the reason whether you agree with intake and why.",
      {
        type: "object",
        additionalProperties: false,
        required: ports.handoffs ? ["reason"] : ["reason", "proposed_workflow"],
        properties: { reason: { type: "string" }, proposed_workflow: { type: "string" } },
      },
      async (args) =>
        withBook(ctx, ports, (book) => {
          const proposed = typeof args["proposed_workflow"] === "string" ? ` [proposed_workflow: ${args["proposed_workflow"]}]` : "";
          const concluded = book.conclude(`${String(args["reason"] ?? "")}${proposed}`);
          return concluded.concluded || concluded.parked !== undefined ? ok([concluded]) : refused(concluded.said);
        }),
    ),
  ];
  if (ports.handoffs) {
    tools.push(
      local(
        "handoff_ir",
        "Escalate one proven hypothesis to incident response, mid-hunt. Refused on anything not proven. The hunt carries on with the rest.",
        { type: "object", additionalProperties: false, required: ["hypothesis_id", "rationale"], properties: { hypothesis_id: { type: "string" }, rationale: { type: "string" } } },
        async (args) =>
          withBook(ctx, ports, (book) => {
            const done = book.handoff(String(args["hypothesis_id"] ?? ""), String(args["rationale"] ?? ""));
            return done.refused ? refused(done.said) : ok([done]);
          }),
      ),
    );
  }
  return tools;
}

// The integration a record's rows came from when they cite no alert (#2287): the server
// that answered every estate query since the hunt's last record, when one alone did. A
// Vigil tool among them, or a second server, leaves it unsaid rather than guessed.
export function servedSince(calls: readonly CallPayload[], tools: readonly ToolSpec[]): string | null {
  const own = agentCalls(calls);
  const since = own.slice(own.map((call) => call.tool).lastIndexOf("record_evidence") + 1);
  const servers = new Set<string | null>();
  for (const call of since) {
    const spec = tools.find((tool) => tool.id === call.tool);
    if (!call.ok || spec === undefined || typeof spec["provides"] !== "string") continue;
    servers.add(typeof spec["server"] === "string" && spec["server"] !== "" ? spec["server"] : null);
  }
  const [only] = servers;
  return servers.size === 1 && only !== undefined ? only : null;
}

// The estate calls that answered from the turn of the hunt's last record up to, not including,
// `turn`, the record's own: the results a record written now could have read (#2520), which a
// conclusion cites by ref and the guard checks it against. A call earlier in the record's own
// batch is left out -- its result reaches the model only on the next turn.
export function servedRefsSince(calls: readonly CallPayload[], turn: number): string[] {
  const own = agentCalls(calls);
  const from = own.findLast((one) => one.tool === "record_evidence" && one.ok)?.turn ?? 0;
  const since = own.filter((call) => call.turn >= from && call.turn < turn);
  return since.filter((call) => !OWN.has(call.tool) && call.tool !== SKILL_TOOL && classOf(call) === "served").map((call) => call.ref);
}

// The expand tool reads raw payloads back off this run's own ledger.
function expandTool(ctx: PluginContext): RegisteredTool[] {
  const specs = ctx.options.spec.tools.filter((tool) => tool.id === "expand");
  return specs.length === 0 ? [] : toolsFrom(specs, { expand: expandFrom(state(ctx), ctx.options.run_id) });
}

// Experimental guards (core/guards.ts). A record names the result it came
// from, and the code -- not the decider, which never read the rows -- fills in
// what the critic argues against: the guarded view of the result, and who wrote
// each field as the reader labelled it. Only that labelling can make a field
// sensor-attested: a field the reader did not label, and a record naming no result,
// count as the adversary's whatever the decider says.
function withRef(item: Record<string, unknown>): Record<string, unknown> {
  const properties = (item["properties"] ?? {}) as Record<string, unknown>;
  return {
    ...item,
    properties: {
      ...properties,
      ref: {
        type: "string",
        description: "The ref of the result this finding came from, such as r4. The result is kept with the record for the critic; without a ref, nothing in the record counts as sensor-attested.",
      },
    },
  };
}

const unattested = (record: Record<string, unknown>): Record<string, unknown> => {
  const asked = Array.isArray(record["rests_on"]) ? (record["rests_on"] as { field?: unknown }[]) : [];
  return {
    ...record,
    ...(asked.length > 0 ? { rests_on: asked.map((basis) => ({ field: String(basis.field ?? ""), authored: "adversary" })) } : {}),
    attacker_influenceable: true,
  };
};

async function fromRefs(ctx: PluginContext, given: readonly Record<string, unknown>[]): Promise<Record<string, unknown>[]> {
  if (!given.some((one) => typeof one["ref"] === "string")) return given.map(unattested);
  // By the ref the decider was shown (ref="r7"), which is the one it cites, and by
  // the ledger's own as a fallback.
  const calls = new Map<string, CallPayload>();
  for (const event of await state(ctx).read(ctx.options.run_id)) {
    if (event.kind !== "call") continue;
    const call = event.payload as CallPayload;
    calls.set(call.ref, call);
    calls.set(guardedRef(call), call);
  }
  return given.map((one) => {
    const { ref, ...rest } = one;
    const call = typeof ref === "string" ? calls.get(ref) : undefined;
    if (call === undefined) return unattested(rest);
    const read = readOf(call.read);
    // Sensor-attested only when the reader said observed AND the value has a shape no
    // free text has (core/guards.ts typeSafe): code, not a model, decides what can
    // let a lead be dropped or a claim be proven.
    const AS = { observed: "sensor", subject: "adversary", outside: "third_party" } as const;
    const labels = new Map(
      (read?.findings ?? [])
        .flatMap((finding) => finding.values)
        .map((value) => [value.field.toLowerCase(), value.authored === "observed" && !typeSafe(value.value) ? "adversary" : AS[value.authored]] as const),
    );
    const asked = Array.isArray(rest["rests_on"]) ? (rest["rests_on"] as { field?: unknown; authored?: unknown }[]) : [];
    const rests_on =
      asked.length > 0
        ? asked.map((basis) => ({ field: String(basis.field ?? ""), authored: labels.get(String(basis.field ?? "").toLowerCase()) ?? "adversary" }))
        : [...labels].map(([field, authored]) => ({ field, authored }));
    return {
      ...rest,
      // What the critic argues against: the decider's own typed view of the rows --
      // sensor-shaped values verbatim, anything the subject could write as a handle.
      // The raw rows stay on the call event for a person; free text an attacker wrote
      // reaches no model that decides or judges.
      payload: call.shown ?? call.result,
      ...(rests_on.length > 0 ? { rests_on, attacker_influenceable: rests_on.every((basis) => basis.authored !== "sensor") } : {}),
    };
  });
}

// Only a plugin whose conclusion carries a disposition waits for the answer after conclude_hunt.
function asksDisposition(ctx: PluginContext): boolean {
  const { conclusion } = ctx.options.plugin;
  return isStructured(conclusion) && "disposition" in ((conclusion.schema["properties"] ?? {}) as Record<string, unknown>);
}

function guarded(ctx: PluginContext): boolean {
  return guardsOn() && ctx.options.plugin.quarantine !== undefined;
}


// The critic already ruled on everything now linked to this hypothesis, and said
// the benign explanation stands: asking again over the same evidence asks the same
// question, and pays for the same answer. Refused, with what would change it.
function unchangedSinceCritic(projection: Projection, hypothesisId: string): string | null {
  const argued = [...projection.evidence.values()].filter((record) => record.provenance === "null_check" && record.payload["hypothesis_id"] === hypothesisId);
  const last = argued[argued.length - 1];
  if (last === undefined || last.payload["survives"] === true) return null;
  const seen = new Set(Array.isArray(last.payload["argued_evidence_ids"]) ? (last.payload["argued_evidence_ids"] as string[]) : []);
  const linked = projection.links
    .filter((link) => link.hypothesis_id === hypothesisId && link.relation !== "neither")
    .map((link) => link.evidence_id)
    .filter((id) => projection.evidence.get(id)?.provenance !== "null_check");
  if (linked.some((id) => !seen.has(id))) return null;
  const benign = String(last.payload["strongest_benign_explanation"] ?? "").slice(0, 300);
  return `${hypothesisId}: the critic already ruled on everything linked to it, and its benign explanation stood: "${benign}". Asking again over the same evidence gets the same answer. Record and link evidence that bears on that explanation -- something it predicts that you can check -- or abandon the hypothesis citing evidence, or leave it unresolved.`;
}
