// A root-cause run: trace one confirmed compromise back to how it began. One
// investigator in one conversation -- search, write to the findings notebook, repeat,
// then report -- with the checks in code rather than in the prompt: a cause is proven
// only by a value both events carry, an origin only by nothing earlier in the logs,
// and a report is sent back while a why is open or it names an actor the proven chain
// does not reach. Rule piles, phase machines and per-step forms were tried before
// this and made it worse; what is left is the lean loop and the checks.
import { createHash } from "node:crypto";
import type { CheckpointPayload, NewEvent, ResolutionPayload, RunKind, RunOutcome } from "../../contracts/events.js";
import { announceOpen, noAnnounce, type Announce } from "../../core/checkpoints.js";
import type { Harness, TurnConfig } from "../../core/loop.js";
import type { Message } from "../../core/provider.js";
import { registryOf } from "../../core/registry.js";
import { SpecError, type RunSpec, type ToolSpec } from "../../core/spec.js";
import { drain, streamTurn } from "../../core/stream.js";
import { remoteTool } from "../../tools/remote.js";
import { Findings, type Lookups } from "./findings.js";
import { isSplunk, quote, splunkSiem, type Siem } from "./siem.js";
import { renderSurvey, scopeOf, startsOf, survey, type Survey } from "./survey.js";
import { findingsTool, splunkQueryTool, type ToolContext } from "./tools.js";
import { RCA_ACTIONS, RCA_PERMIT, type RcaKinds, type ReportEmission, type SegmentPayload } from "./vocabulary.js";

type Event = NewEvent<RcaKinds>;

export interface RcaOptions {
  run_id: string;
  run_kind: RunKind;
  spec: RunSpec;
  started_by?: string;
  // How a human is told the run is waiting on a permit. Defaults to nobody, and a
  // checkpoint nobody is told about is one nobody answers.
  announce?: Announce;
  signal?: AbortSignal;
}

export interface RcaReport {
  status: RunOutcome | "waiting_approval";
  reason: string;
}

// Stretches of the investigation, each ended by a report draft the finish gate
// judges. A refusal is carried into the next one; the last draft stands when they run out.
export const DEFAULT_SEGMENTS = 5;
// Refusals stop once this much of the cost ceiling is gone, so a run always has
// room left to write the report it is going to be held to.
const REFUSE_UNTIL = 0.8;
// A notebook still empty after this many searches gets reminded a hypothesis needs no proof.
const EMPTY_NUDGE_AFTER = 8;

export async function runRootCause(harness: Harness<RcaKinds>, options: RcaOptions): Promise<RcaReport> {
  const { run_id, spec } = options;
  const lead = spec.roles.lead;
  if (lead === undefined || lead.output_schema === null) throw new SpecError(`arch ${spec.arch} declares no investigator that reports`);
  if ((await harness.state.latestSeq(run_id)) === null) await open(harness, options);
  if ((await harness.state.terminal(run_id)) !== null) return { status: "completed", reason: "the run already ended" };

  // Said before anything is spent: the checks are SPL, and a deployment whose log
  // search is not Splunk's splunk_execute cannot answer them.
  const bound = telemetryOf(spec);
  if (bound === undefined || !isSplunk(bound.id)) {
    const reason =
      bound === undefined
        ? "a root-cause trace needs Splunk (splunk_execute), and this deployment has no log search bound"
        : `a root-cause trace needs Splunk (splunk_execute); this deployment searches logs with ${bound.id}, which cannot run its checks`;
    return end(harness, options, "failed", reason);
  }

  const events = await harness.state.read(run_id);
  const permit = await permitted(harness, options, events);
  if (permit !== null) return permit;

  const siem = splunkSiem(remoteTool(bound), harness.dispatch);
  const surveyed = events.find((event) => event.kind === "survey")?.payload as Survey | undefined;
  const found = surveyed ?? (await survey(siem, options.signal));
  if (surveyed === undefined) await append(harness, options, [event(options, "survey", found)]);
  if (found.error !== undefined) return end(harness, options, "failed", `the log store could not be surveyed: ${found.error}`);

  const findings = new Findings(lookupsFor(siem, found, options.signal));
  let searches = 0;
  for (const one of events) {
    if (one.kind !== "query") continue;
    findings.saw((one.payload as RcaKinds["query"]).lines);
    searches += 1;
  }
  const notebook = events.filter((one) => one.kind === "notebook").at(-1);
  if (notebook !== undefined) await findings.restore(notebook.payload as RcaKinds["notebook"]);
  const segments = events.filter((one) => one.kind === "segment").map((one) => one.payload as SegmentPayload);

  const context: ToolContext = {
    siem,
    survey: found,
    findings,
    record: async (shown) => {
      searches += 1;
      await append(harness, options, [event(options, "query", shown)]);
    },
    footer: () => {
      if (findings.open().length > 0) return findings.line();
      if (findings.list.length === 0 && findings.hyps.length === 0 && searches >= EMPTY_NUDGE_AFTER && searches % 4 === 0) {
        return "Your findings file is empty. If you have a working theory, record it now as a hypothesis (findings op=hypothesis: what you think happened, and the search that would test it); write findings as the events prove them.";
      }
      return "";
    },
  };
  const save = () => append(harness, options, [event(options, "notebook", findings.snapshot())]);
  const index = found.sources[0]?.index ?? "main";
  const tools = [splunkQueryTool(context, index), findingsTool(context, save)];
  const own: Harness<RcaKinds> = { ...harness, registry: registryOf(tools, { lead: tools.map((tool) => tool.id) }) };

  const ceiling = spec.thresholds["max_iterations"] ?? DEFAULT_SEGMENTS;
  const task = brief(spec, found);
  let history: Message[] = segments.flatMap((one) => one.transcript);
  let draft = segments.map((one) => one.draft).filter((one): one is string => one !== null).at(-1) ?? null;
  // Accepted, then the process died before the terminal: the report already stands.
  const last = segments.at(-1);
  if (last !== undefined && last.refused === null && last.draft !== null) {
    return end(harness, options, "completed", `reported after ${last.segment} stretch(es), ${findings.list.length} finding(s)`, findings.redact(last.draft));
  }

  for (let segment = segments.length + 1; segment <= ceiling; segment += 1) {
    const outcome = await drain(streamTurn<ReportEmission, RcaKinds>(turnFor(options, lead.prompt, task, lead.output_schema, history), own));
    if (outcome.status === "waiting_approval") {
      if (outcome.pending !== null) await announceOpen(harness.state, run_id, options.run_kind, outcome.pending.checkpoint_id, options.announce ?? noAnnounce);
      return { status: "waiting_approval", reason: outcome.reason };
    }
    if (outcome.status === "failed" || outcome.value === null) {
      if (outcome.refusal === null) return end(harness, options, "failed", outcome.reason);
      return draft === null
        ? end(harness, options, "budget_exhausted", outcome.reason, fallback(findings))
        : end(harness, options, "budget_exhausted", outcome.reason, findings.redact(draft));
    }

    draft = outcome.value.report;
    const spent = harness.budget.spent.cost_usd;
    const budgetLeft = segment < ceiling && spent < harness.budget.limits.max_cost_usd * REFUSE_UNTIL;
    const refused = findings.gate(draft, budgetLeft) ?? null;
    const added = outcome.transcript.slice(history.length);
    const carried: Message[] =
      refused === null
        ? added
        : [...added, { role: "assistant", content: JSON.stringify(outcome.value), tool_calls: [] }, { role: "user", content: `${refused} Run the searches that settle it, then report again.` }];
    await append(harness, options, [
      event(options, "segment", { segment, transcript: carried, draft, refused }),
      event(options, "notebook", findings.snapshot()),
    ]);
    if (refused === null) return end(harness, options, "completed", `reported after ${segment} stretch(es), ${findings.list.length} finding(s)`, findings.redact(draft));
    history = [...history, ...carried];
  }

  const unanswered = findings.open().length;
  const reason = `ran out of stretches with ${unanswered} why${unanswered === 1 ? "" : "s"} open; the last report stands`;
  return end(harness, options, "completed", reason, draft === null ? fallback(findings) : findings.redact(draft));
}

// The first capability-bound log search the config carries, as its declared spec.
function telemetryOf(spec: RunSpec): ToolSpec | undefined {
  return spec.tools.find((tool) => tool["provides"] === "telemetry_search");
}

// Ask once, then wait. A run teed up by a hunt's escalation has no operator behind
// it yet, so it spends nothing until someone permits the trace of this finding.
async function permitted(
  harness: Harness<RcaKinds>,
  options: RcaOptions,
  events: readonly { kind: string; payload: unknown }[],
): Promise<RcaReport | null> {
  const policies = options.spec.sections["checkpoints"];
  const policy = typeof policies === "object" && policies !== null ? (policies as Record<string, unknown>)[RCA_PERMIT] : undefined;
  // Anything but auto asks, so a typo can never switch the gate off.
  if (policy === undefined || policy === "auto") return null;

  const checkpoint_id = `rca-permit-${createHash("sha256").update(options.run_id).digest("hex").slice(0, 12)}`;
  const raised = events.some((one) => one.kind === "checkpoint" && (one.payload as CheckpointPayload).checkpoint_id === checkpoint_id);
  if (!raised) {
    const payload: CheckpointPayload = {
      checkpoint_id,
      checkpoint_class: RCA_PERMIT,
      question: `Permit a root-cause trace of this finding back to how it began?${subjectOf(options.spec)}`,
      raised_at: new Date().toISOString(),
    };
    await harness.state.append(options.run_id, [event(options, "checkpoint", payload)]);
  }
  const answer = events.find((one) => one.kind === "resolution" && (one.payload as ResolutionPayload).checkpoint_id === checkpoint_id)?.payload as
    | ResolutionPayload
    | undefined;
  if (answer === undefined) {
    await announceOpen(harness.state, options.run_id, options.run_kind, checkpoint_id, options.announce ?? noAnnounce);
    return { status: "waiting_approval", reason: `waiting for someone to permit the trace (${checkpoint_id})` };
  }
  if (answer.answer === "approve") return null;
  return end(harness, options, "aborted", `${answer.actor} did not permit the trace${answer.text ? `: ${answer.text}` : ""}`);
}

// What the question names, so the inbox says which finding rather than "a run".
// The prompt is markdown the target context built ("**Additional Context:** …"); an
// inbox row shows it as plain text, so the emphasis and its labels go.
function subjectOf(spec: RunSpec): string {
  const text = spec.prompt
    .replace(/\*\*[^*]+:\*\*/g, "")
    .replace(/[*_`#]+/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (text === "") return "";
  return ` ${text.length > 300 ? `${text.slice(0, 300)}…` : text}`;
}

// The checks' questions to the store, over every index the survey found.
function lookupsFor(siem: Siem, found: Survey, signal?: AbortSignal): Lookups {
  const scope = scopeOf(found);
  // The exact value, not part of a longer name.
  const exact = (value: string) => `(?<![\\w.-])${value.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")}(?![\\w-])`;
  const count = async (spl: string, latest?: number) => {
    const searched = await siem.search(spl, latest === undefined ? {} : { latest }, 1, signal);
    const row = searched.rows[0];
    return searched.error !== undefined || row === undefined ? undefined : Number(row["count"] ?? 0);
  };
  return {
    starts: startsOf(found),
    countBefore: (value, before) => count(`${scope} ${quote(value)} | regex _raw=${quote(exact(value))} | stats count`, before),
    mentions: (value, before) => count(`${scope} ${quote(value)} | stats count`, before),
    sourcesOf: async (value) => {
      const searched = await siem.search(`${scope} ${quote(value)} | stats count by sourcetype`, {}, 100, signal);
      return searched.rows.map((row) => String(row["sourcetype"] ?? "")).filter(Boolean);
    },
  };
}

// The job's own subject first, then the definition's framing, then every source.
function brief(spec: RunSpec, found: Survey): string {
  return [
    spec.prompt && `## What this run traces\n\n${spec.prompt}`,
    spec.narrative,
    renderSurvey(found),
  ]
    .filter((part) => part)
    .join("\n\n");
}

function turnFor(options: RcaOptions, system: string, task: string, schema: Record<string, unknown>, history: readonly Message[]): TurnConfig {
  const { runtime } = options.spec;
  return {
    run_id: options.run_id,
    run_kind: options.run_kind,
    role: "lead",
    system,
    task,
    schema,
    history,
    max_turns: runtime.max_turns,
    approvals: new Set(options.spec.approvals),
    verbs: RCA_ACTIONS,
    result_cap: runtime.result_cap,
    recall_limit: runtime.recall_limit,
    ...(options.signal === undefined ? {} : { signal: options.signal }),
  };
}

// No report was ever drafted: the notebook is what the run found.
function fallback(findings: Findings): string {
  return findings.redact(`No report was written before the run stopped. The findings file as it stood:\n\n${findings.table()}`);
}

function event(options: RcaOptions, kind: Event["kind"], payload: Event["payload"]): Event {
  return { run_id: options.run_id, run_kind: options.run_kind, kind, payload } as Event;
}

async function append(harness: Harness<RcaKinds>, options: RcaOptions, own: readonly Event[]): Promise<void> {
  await harness.state.append(options.run_id, own);
}

async function open(harness: Harness<RcaKinds>, options: RcaOptions): Promise<void> {
  await append(harness, options, [
    event(options, "run", {
      run_kind: options.run_kind,
      spec: options.spec,
      budgets: harness.budget.limits,
      seed: options.run_id,
      tenant_id: null,
      started_by: options.started_by ?? "worker",
    }),
  ]);
}

// A failure already lands in the error column, so it leaves the summary unset.
async function end(harness: Harness<RcaKinds>, options: RcaOptions, outcome: RunOutcome, reason: string, summary?: string): Promise<RcaReport> {
  const payload = summary === undefined || outcome === "failed" ? { outcome, reason } : { outcome, reason, summary };
  await append(harness, options, [event(options, "terminal", payload)]);
  return { status: outcome, reason };
}

