// Model calls (spec §5.2). A move is one kernel turn: the model searches up to the move's cap through the evidence
// tool, then emits the move's submission against its schema. The kernel journals every search as a `call` event
// with a ref, bills the model, delimits the results and resumes; what is left here is the move's own check, once.
import { runSubturn, type PluginContext, type PluginHooks, type Subturn, type AgentKinds, type AgentOptions } from "../../../../core/agent.js";
import { schemaErrors, schemaIssues } from "../../../../core/stream.js";
import type { CallPayload, RunKind } from "../../../../contracts/events.js";
import { defineTool, type RegisteredTool, type ToolResult } from "../../../../contracts/tool.js";
import type { Harness, Outcome } from "../../../../core/loop.js";
import type { PluginManifest } from "../../../../contracts/plugin.js";
import type { RunSpec } from "../../../../core/spec.js";
import type { AgentConfig } from "../config.js";
import type { Move } from "../frontier.js";
import { issued, neverRan, type EvidenceTool, type Registry, type ToolRegistry } from "../tools/registry.js";
import type { TurnTrace } from "../trace.js";
import type { Domain } from "../types.js";
import { lengthLimits, SUBMIT, SUBMIT_DESCRIPTIONS, type SubmitName } from "./schemas.js";
import { SYSTEM_PROMPT } from "./prompts.js";
import { baselineProblem, type InputOf } from "../baseline.js";

export type TurnMove = Move | "pivot-entry" | "pivot-forward" | "scope";

export interface TurnRequest {
  move: TurnMove;
  correlationId: string;
  userContent: string;
  submitTool: SubmitName;
  allowedDomains: Domain[];
  focusTime?: string; // the target's time: the default search window is built around it
  beforeMs?: number; // look-back of the default window
  maxSearches?: number; // this move's search cap, below cfg.maxToolCallsPerTurn (strengthen needs few)
  minCount?: number; // mapping mode for the search tool
  trace: TurnTrace;
}

export interface ToolCallOut { id: string; tool: string; input: unknown; ok: boolean; traceId: string; text?: string }

export interface TurnResult {
  submission: unknown | null;
  toolCalls: ToolCallOut[];
  failed?: string; // the provider or gateway failed this turn (its error text): the model's answer was never judged
}

export interface ModelClient {
  runTurn(req: TurnRequest): Promise<TurnResult>;
  addContext?(text: string): void; // appended to the system prompt (the environment map), cached with it
}

export interface ClientDeps { cfg: AgentConfig; tools: ToolRegistry; domains: Domain[]; survey: string; range: [string, string] }

// The pool refused another model call: the run's allowance (calls, dollars, wall clock) is spent.
export class Refused extends Error {}

// Runs one evidence tool call the code makes (a script's, a sweep's) and traces it: the query, then the output.
export async function runTool(d: ClientDeps, req: TurnRequest, tool: string, input: unknown): Promise<ToolCallOut> {
  const rec = await d.tools.call(tool, input, { allowedDomains: req.allowedDomains, allDomains: d.domains, timeoutMs: d.cfg.toolTimeoutMs, range: d.range, focusTime: req.focusTime, beforeMs: req.beforeMs, minCount: req.minCount });
  req.trace.write("query", { toolCallId: rec.toolCallId, tool, input });
  const text = rec.result.ok ? rec.result.data : rec.result.error;
  const traceId = req.trace.write("tool-output", { toolCallId: rec.toolCallId, ok: rec.result.ok, output: text });
  return { id: rec.toolCallId, tool, input, ok: rec.result.ok, traceId, text };
}

// Over-long text is trimmed, not refused: a long note must not throw away a correct finding. The caps are the schemas'.
export const LIMITS = lengthLimits(Object.values(SUBMIT));
const trim = (v: string, max: number | undefined) => (max && v.length > max ? v.slice(0, max - 1) + "…" : v);
// A list or object sent as a JSON string (models do send events that way) is parsed, not refused.
const STRUCT = new Set(["takeover", "candidates", "effects", "causes", "events", "evidence", "nodeEvidence", "edgeEvidence", "artifacts", "identitySignals", "baseline", "boundary", "theories", "predictions", "nextTests", "toolCallIds", "supports", "weakens", "links", "unrelated", "contradicts"]);
const unstring = (v: string): unknown => { try { const x = JSON.parse(v); return x && typeof x === "object" ? x : v; } catch { return v; } };
export const clip = (v: unknown, key = ""): unknown =>
  typeof v === "string" && STRUCT.has(key) && /^\s*[[{]/.test(v) && unstring(v) !== v ? clip(unstring(v), key)
  : typeof v === "string" ? trim(v, LIMITS.scalar[key])
  : Array.isArray(v) ? v.map((x) => (typeof x === "string" ? trim(x, LIMITS.items[key]) : clip(x)))
  : v && typeof v === "object" ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, clip(x, k)])) : v;

// Why a clipped value does not meet the move's schema (what the kernel's ajv holds an emission to, plus the length
// caps), with the list items the failure rests on, from the paths ajv reports: the deepest list item on each path
// (`effects/0/artifacts/1`, not `effects/0`), so the rest of the answer survives. null when it does meet it.
const itemOf = (p: string[]) => p.slice(0, p.findLastIndex((x) => /^\d+$/.test(x)) + 1);
function schemaProblem(name: SubmitName, value: unknown): { msg: string; drop: Path[] } | null {
  const msg = schemaErrors(SUBMIT[name], value);
  if (msg === null) return null;
  const paths = msg.split("; ").map((e) => e.split(" ")[0]!.split("/").slice(1));
  const drop = paths.map(itemOf).filter((p) => p.length);
  return { msg, drop: drop.length === paths.length ? drop : [] };
}

// The kernel's ajv has held the emission to the schema already; this clips it, then checks citations and the rest.
// Every toolCallId must be a search the run journaled. A failure is not sent back to the model: salvage keeps what
// is sound and the move's attempt is spent.
type Cand = { label: string; baseline?: { unusual: boolean; toolCallIds: string[] } };
// No "recon": it names resources too ("recon job"), and refusing on it lost a correct finding.
export const ACCUSE = /\b(attacker|attacker's|malicious|adversary|threat actor|stolen|compromised|hijacked|backdoor|exfiltrat\w*|intruder|rogue|abus\w*|tamper\w*)\b/i;

const LISTS = ["candidates", "effects", "causes", "events"] as const;
// Attribution: saying an account or credential was taken over (stolen, compromised, controlled by someone
// else, the attacker using or operating it) needs the record of the takeover itself. Runs have blamed a
// legitimate person this way, from a missing login record or from sharing their address.
export const TAKEOVER = /\b(stolen|compromised|hijack\w*|taken over|took over|account takeover|controlled by (?:the )?(?:attacker|adversary|an? (?:external|unknown) (?:operator|attacker))|(?:attacker|adversary|intruder|operator)(?:'s)? (?:us(?:ing|ed)|operat(?:ing|ed)|controll(?:ing|ed)|with|holding|via)\b|(?:attacker|adversary)-(?:controlled|operated))/i;
const ABSENCE = /\b(no|not|never|without|absen\w*|missing|lack\w*)\b[^.]{0,60}\b(record|log|login|logon|sign-?in|assume-?role|federation|token|event|found|seen|recorded)/i;
const takeoverProblem = (c: { label: string; takeover?: { summary: string; toolCallIds: string[] } }) =>
  !TAKEOVER.test(c.label) ? null
  : !c.takeover?.toolCallIds.length ? `"${c.label.slice(0, 80)}": the label says an account or credential was taken over (stolen, compromised, controlled or used by the attacker) but gives no takeover record. Either cite the record of the takeover itself under takeover (a sign-in from a new address or device, a token, key or password created or reset, an MFA change, a credential read from a host or file, malicious code running in that account's session (the process record)), or relabel it neutrally: what happened, run as which account, from which address. A missing login or assume-role record is not a takeover record, and sharing an address or role with the attacker is not one either.`
  : ABSENCE.test(c.takeover.summary) ? `"${c.label.slice(0, 80)}": the takeover record must be a record that exists; "${c.takeover.summary.slice(0, 100)}" describes a missing one. Relabel it neutrally (what happened, run as which account, from which address) unless a search returns the takeover itself.`
  : null;
const STORY_TAKEOVER = /\b(insider|rogue (?:employee|admin|developer|user))\b/i;
const TAKEOVER_TEST = /\b(sign-?in|login|logon|token|key|password|mfa|credential|session (?:creat|issu)|assume-?role|federat)/i;
export const UNSEEN = /\b(outside (?:the )?(?:captured |available |collected )?(?:telemetry|logs?|logging|data|visibility)|(?:not|never|un) ?(?:captured|logged|recorded|observed|visible) (?:in|by) (?:the )?(?:telemetry|logs?|data|any source)|unlogged|unobserved|out[- ]of[- ]band (?:mint|issu)\w*|no visible (?:mint|issu|origin|source)\w*)\b/i;
export interface CheckCtx { inputOf?: InputOf; range?: [string, string] }
type Path = (string | number)[];
// drop: the paths of the list items a failure rests on (["effects", "0", "artifacts", "1"]), so a last salvage can keep the rest of the submission.
export type CheckResult = { success: true; data: unknown } | { success: false; msg: string; drop: Path[] };

type Citable = Parameters<typeof issued>[0];
export function check(name: SubmitName, input: unknown, calls: number | Citable, cx: CheckCtx = {}): CheckResult {
  const tools = typeof calls === "number" ? { callCount: calls } : calls;
  const v = { data: clip(input) };
  // A list item citing a ref the run never issued fails the check and salvage drops it, the rest kept; a bad
  // citation outside any list fails the submission whole.
  const unissued = (x: unknown): string[] => Array.isArray(x) ? x.flatMap(unissued) : x && typeof x === "object" ? Object.entries(x).flatMap(([k, y]) => (k === "toolCallIds" && Array.isArray(y) ? y.map(String).filter((id) => !issued(tools, id)) : unissued(y))) : [];
  const top = Object.entries(v.data as Record<string, unknown>);
  const dropped: Path[] = top.flatMap(([k, x]) => (Array.isArray(x) ? x.flatMap((item, i) => (unissued(item).length ? [[k, i]] : [])) : []));
  const loose = top.flatMap(([, x]) => (Array.isArray(x) ? [] : unissued(x)));
  if (dropped.length || loose.length) {
    const ids = new Set([...loose, ...dropped.flatMap(([k, i]) => unissued((v.data as unknown as Record<string, unknown[]>)[k as string]![i as number]))]);
    return { success: false, msg: `toolCallIds never issued in this run: ${[...ids].join(", ")}. Cite only refs from your searches or from the snapshot.`, drop: loose.length ? [] : dropped };
  }
  // The environment map records what exists and how often, never a judgment about it.
  if (name === "submit_environment") {
    const m = String((v.data as { map?: string }).map ?? "").match(/\b(suspicious|malicious|anomal\w*|outliers?|unusual|attack\w*|attacker|c2|command[- ]and[- ]control|phish\w*|not normal|abnormal|compromise\w*|exfiltrat\w*|backdoor\w*|indicator\w*)\b/gi);
    if (m) return { success: false, drop: [], msg: `the map judges activity (${[...new Set(m.map((x) => x.toLowerCase()))].join(", ")}). It records only what exists and how often ("seen N times on M hosts"). Remove those judgments and anything seen only a few times, and submit again.` };
  }
  // Any event labelled as the attacker's (or malicious, stolen, compromised) must carry a baseline showing it is
  // not that account's or address's normal activity, whichever move proposes it. Otherwise: fix it once.
  const d = v.data as unknown as Partial<Record<(typeof LISTS)[number], Cand[]>>;
  const items = LISTS.flatMap((k) => (d[k] ?? []).map((c, i) => ({ c, at: [k, i] as [string, number] })));
  const taken = items.map((x) => ({ ...x, why: takeoverProblem(x.c as never) })).filter((x) => x.why);
  if (taken.length) return { success: false, drop: taken.map((x) => x.at), msg: taken.map((x) => x.why).join(" ") };
  // A story may suppose a takeover or an insider, but then one of its predictions must look for the takeover record.
  if (name === "submit_theories") {
    const ts = (v.data as { theories: { id: string; story: string; predictions: { expect: string; spl: string }[] }[] }).theories;
    const bad = ts.filter((t) => (TAKEOVER.test(t.story) || STORY_TAKEOVER.test(t.story)) && !t.predictions.some((p) => TAKEOVER_TEST.test(`${p.expect} ${p.spl}`)));
    if (bad.length) return { success: false, drop: [], msg: `${bad.map((t) => t.id).join(", ")}: the story says an account was taken over or that a person is the attacker, but no prediction looks for the record of that takeover. Add a prediction that would find it (the sign-in from a new address or device, the token, key or password created, the credential read), or tell the story without naming whose account was taken.` };
  }
  const accusing = items.filter(({ c }) => ACCUSE.test(c.label) && !(c.baseline?.unusual && c.baseline.toolCallIds.length));
  // An accusing label without a baseline, or a baseline that fails the checks, is kept and tagged unverified
  // (the report never accuses from it; a failed baseline proves nothing either way, so it is dropped).
  for (const { c } of accusing) (c as Cand & { unverified?: boolean }).unverified = true;
  for (const { c } of items) if (baselineProblem(c as never, cx.inputOf, cx.range)) { (c as Cand & { unverified?: boolean }).unverified = true; delete c.baseline; }
  // An event is a record the searches returned. "Happened outside the logs" is not one: it ends the chain on an
  // absence and stops the search for the real step ("credential minted outside captured
  // telemetry" was once confirmed and became the chain top; the token theft upstream was never looked for).
  const unseen = items.filter(({ c }) => UNSEEN.test(c.label));
  if (unseen.length) return { success: false, drop: unseen.map((x) => x.at), msg: `${unseen.map(({ c }) => `"${c.label.slice(0, 80)}"`).join("; ")}: an event must be a record your searches returned, not something inferred from records that are missing. Leave it out and report the records that do exist: earlier records naming the same credential, key, token, session, file, host or pod (where it was read, written, copied or used first). If nothing in the data shows the cause, report no candidate for it (or a boundary to the source that would record it).` };
  return { success: true, data: v.data };
}


// v without the list items at the paths in gone ("effects/0/artifacts/1"); the rest keep their place.
const prune = (v: unknown, at: string[], gone: Set<string>): unknown =>
  Array.isArray(v) ? v.flatMap((x, j) => (gone.has([...at, j].join("/")) ? [] : [prune(x, [...at, String(j)], gone)]))
  : v && typeof v === "object" ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, prune(x, [...at, k], gone)])) : v;

// Where the value at path p (pre-prune) is once prune has taken out gone: null when it, or a list item holding it, went.
const follow = (p: string[], gone: Set<string>): string[] | null => {
  const out: string[] = [];
  for (let n = 0; n < p.length; n++) {
    if (!/^\d+$/.test(p[n]!)) { out.push(p[n]!); continue; }
    if (gone.has(p.slice(0, n + 1).join("/"))) return null;
    let i = +p[n]!;
    for (let j = +p[n]! - 1; j >= 0; j--) if (gone.has([...p.slice(0, n), j].join("/"))) i--;
    out.push(String(i));
  }
  return out;
};

// A key the schema does not know is removed, not a reason to drop the item or the submission. The deepest list item
// holding a real error is dropped whole by the caller, so nothing is stripped from it; the item around it (a
// candidate around a bad nodeEvidence entry) is stripped as any other. One that is a field of the submission itself
// (a candidate's `boundary`) is returned with where it was, for the caller to lift once it knows that item is kept.
// Mutates `cur`.
function stripStray(name: SubmitName, cur: Record<string, unknown>): { stripped: number; lifts: { at: string[]; key: string; value: unknown }[] } {
  const schema = SUBMIT[name] as { properties: Record<string, Record<string, unknown>> };
  const errs = schemaIssues(schema, cur);
  const dead = errs.filter((e) => e.keyword !== "additionalProperties").map((e) => itemOf(e.instancePath.split("/").slice(1)).join("/")).filter(Boolean);
  const live = errs.flatMap((e) => {
    const at = e.instancePath.split("/").slice(1);
    return e.keyword === "additionalProperties" && !dead.some((d) => `${at.join("/")}/`.startsWith(`${d}/`)) ? [{ at, key: String((e.params as { additionalProperty: string }).additionalProperty) }] : [];
  });
  const lifts: { at: string[]; key: string; value: unknown }[] = [];
  for (const { at, key } of live) {
    const parent = at.reduce<any>((v, k) => v[k], cur);
    const value = parent[key];
    delete parent[key];
    if (at.length && key in schema.properties && !schemaIssues(schema.properties[key]!, value).length) lifts.push({ at, key, value });
  }
  return { stripped: live.length, lifts };
}

// A failed check drops the list items it rests on and keeps the rest, so one bad event does not throw away
// the others (a run once lost a correct finding with its whole submission).
export function salvage(name: SubmitName, input: unknown, calls: number | Citable, cx: CheckCtx, first: CheckResult): { data: unknown; dropped: number; repaired?: string } | null {
  let cur = clip(input) as Record<string, unknown[]>, r = first, dropped = 0;
  let { stripped, lifts } = stripStray(name, cur);
  if (stripped) { const bad = schemaProblem(name, cur); r = bad ? { success: false, ...bad } : check(name, cur, calls, cx); }
  for (let i = 0; i < 3 && !r.success; i++) {
    if (!r.drop.length) return null;
    const gone = new Set(r.drop.map((p) => p.join("/")));
    lifts = lifts.flatMap((l) => { const at = follow(l.at, gone); return at ? [{ ...l, at }] : []; });
    cur = prune(cur, [], gone) as typeof cur;
    dropped += gone.size;
    const bad = schemaProblem(name, cur); // dropping can leave a list under its minimum
    r = bad ? { success: false, ...bad } : check(name, cur, calls, cx);
  }
  if (!r.success) return null;
  // Lifted only from an item still there, into an empty slot of a submission that holds without it, and only if
  // the submission still holds with it: a lift never fills a field the submission was refused for lacking.
  const up = new Map<string, unknown>();
  for (const { key, value } of lifts) if (cur[key] === undefined && !up.has(key)) up.set(key, value);
  if (up.size) {
    const next = { ...cur, ...Object.fromEntries(up) };
    const lr = schemaProblem(name, next) ? null : check(name, next, calls, cx);
    if (lr?.success) r = lr;
    else up.clear();
  }
  const repaired = stripped ? `stripped ${stripped} unknown key(s)${up.size ? `, lifted ${[...up.keys()].join(", ")} to the top level` : ""}` : null;
  return { data: r.data, dropped, ...(repaired ? { repaired } : {}) };
}

// What a recorded turn looks like: replayable by ScriptedModelClient against a cassette. Tool call ids in
// the submission are written as $0, $1 ... (the index of the call in this turn).
export interface ScriptEntry { move: TurnMove; correlationId: string; toolCalls: { tool: string; input: unknown }[]; submission: unknown }

const mapIds = (v: unknown, f: (s: string) => string): unknown =>
  typeof v === "string" ? f(v) : Array.isArray(v) ? v.map((x) => mapIds(x, f)) : v && typeof v === "object" ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, mapIds(x, f)])) : v;


// What a move touches of the kernel: the run's harness and options as `runAgent` would hold them, with the evidence
// tool granted as a local tool the model's searches go through. Frame and moves alike run as subturns here.
export class KernelTurns {
  // The tools hooks grant: set once the run has a source to search.
  local: readonly RegisteredTool[] = [];
  private ctx: PluginContext;
  constructor(harness: Harness<AgentKinds>, run: { run_id: string; run_kind: RunKind; spec: RunSpec; plugin: PluginManifest; tools: readonly RegisteredTool[]; started_by?: string; signal?: AbortSignal }) {
    const hooks: PluginHooks = { tools: () => this.local };
    const options: AgentOptions = { ...run, hooks };
    // No outer model loop here: runSubturn hands hooks its own turn counter.
    this.ctx = { harness, options, turn: () => 0 };
  }
  turn<T>(sub: Subturn): Promise<Outcome<T>> { return runSubturn<T>(this.ctx, sub); }
  async calls(): Promise<CallPayload[]> {
    return (await this.ctx.harness.state.read(this.ctx.options.run_id)).filter((e) => e.kind === "call").map((e) => e.payload as CallPayload);
  }
}

// The JSON the kernel holds an emission to. Lengths are not asked of it: an over-long note is trimmed by the
// check, and must not cost the whole submission.
const wire = (name: SubmitName): Record<string, unknown> => {
  const strip = (x: unknown): unknown => Array.isArray(x) ? x.map(strip) : x && typeof x === "object" ? Object.fromEntries(Object.entries(x).filter(([k]) => k !== "maxLength").map(([k, y]) => [k, strip(y)])) : x;
  return { description: SUBMIT_DESCRIPTIONS[name], ...(strip(SUBMIT[name]) as Record<string, unknown>) };
};

const argsOf = (raw: string): unknown => { try { return JSON.parse(raw); } catch { return {}; } };

export class KernelModelClient implements ModelClient {
  private system: string[];
  private cur: TurnRequest | undefined;
  // What the evidence tool answered this move, in order: the raw output the kernel's own result text is cut from.
  private answered: { key: string; ok: boolean; output: string }[] = [];

  constructor(private d: ClientDeps & { tools: Registry }, private turns: KernelTurns) {
    this.system = [SYSTEM_PROMPT, d.survey];
    turns.local = d.tools.list().map((t) => this.localTool(t));
  }

  addContext(text: string) {
    this.system.push(text);
  }

  // The evidence tool as the kernel holds it: its handler is the registry's run, scoped to the move in hand.
  private localTool(t: EvidenceTool): RegisteredTool {
    const { cfg, domains, range } = this.d;
    return defineTool(
      {
        id: t.name,
        description: t.description,
        parameters: t.inputSchema,
        execute: async (args): Promise<ToolResult> => {
          const req = this.cur;
          if (req === undefined) return { ok: false, failure: { kind: "refused", detail: "no move is running" } };
          const r = await this.d.tools.run(t.name, args, { allowedDomains: req.allowedDomains, allDomains: domains, timeoutMs: cfg.toolTimeoutMs, range, focusTime: req.focusTime, beforeMs: req.beforeMs, minCount: req.minCount });
          this.answered.push({ key: JSON.stringify(args), ok: r.ok, output: r.ok ? r.data ?? "" : r.error ?? "" });
          if (!r.ok) {
            const detail = r.error ?? "";
            return { ok: false, failure: /^timed out/.test(detail) ? { kind: "timeout", timeoutMs: cfg.toolTimeoutMs } : /^bad input/.test(detail) ? { kind: "invalid_args", detail } : { kind: "backend_error", detail } };
          }
          const max = cfg.rawOutputMaxChars;
          const data = r.data ?? "";
          return { ok: true, rows: [{ output: data.length > max ? `${data.slice(0, max)}\n…[cut at ${max} chars: aggregate or narrow the search]` : data }], rowCount: 1, capped: false, sourceSystem: t.name };
        },
      },
      { maxRows: 1, timeoutMs: cfg.toolTimeoutMs + 60_000 },
      true,
    );
  }

  async runTurn(req: TurnRequest): Promise<TurnResult> {
    const { cfg, tools } = this.d;
    const searches = req.maxSearches ?? cfg.maxToolCallsPerTurn;
    const before = (await this.turns.calls()).length;
    this.cur = req;
    this.answered = [];
    let outcome: Outcome<unknown> | undefined;
    let failure: unknown;
    try {
      outcome = await this.turns.turn({
        role: req.move,
        system: this.system.join("\n\n"),
        task: req.userContent,
        schema: wire(req.submitTool),
        tools: searches > 0 ? this.turns.local.map((t) => t.id) : [],
        max_turns: searches,
        cite: true,
      });
    } catch (error) {
      failure = error;
    } finally {
      this.cur = undefined;
    }
    // Whatever the move searched is on the ledger and citable, however it ended.
    const toolCalls = await this.settle(req, before);
    if (failure !== undefined) throw failure;
    if (outcome!.refusal !== null) throw new Refused(outcome!.reason);

    // The emitted value is checked once: what is sound is kept, what is not is dropped, and a turn with no
    // valid submission is one attempt on its target.
    // An emission the kernel rejected twice is salvaged too, when it parsed: what in it is sound is kept.
    let submission: unknown = null;
    // A provider failure is not an answer the model got wrong: it is told apart on the ledger and in the result.
    const failed = outcome!.status === "failed" && outcome!.emission_rejected !== true ? outcome!.reason : undefined;
    const kept = outcome!.value ?? outcome!.rejected_value;
    if (failed !== undefined) req.trace.write("provider-failure", { reason: failed });
    else if (kept === undefined || kept === null || typeof kept !== "object") req.trace.write("rejected-submission", { reason: outcome!.reason });
    else {
      const cx: CheckCtx = { inputOf: tools.inputOf?.bind(tools), range: this.d.range };
      const bad = outcome!.value === null ? schemaProblem(req.submitTool, clip(kept)) : null;
      const v: CheckResult = bad ? { success: false, ...bad } : check(req.submitTool, kept, tools, cx);
      if (v.success) submission = v.data;
      else {
        const s = salvage(req.submitTool, kept, tools, cx, v);
        req.trace.write("rejected-submission", { input: kept, error: v.msg, ...(s ? { salvaged: `kept the rest, dropped ${s.dropped} item(s)`, ...(s.repaired ? { repaired: s.repaired } : {}) } : {}) });
        submission = s?.data ?? null;
      }
    }
    finishTurn(req, submission);
    return { submission, toolCalls, ...(failed !== undefined ? { failed } : {}) };
  }

  // The move's searches as the ledger holds them: citable from here on, and traced for the report.
  private async settle(req: TurnRequest, before: number): Promise<ToolCallOut[]> {
    const out: ToolCallOut[] = [];
    let at = 0;
    for (const call of (await this.turns.calls()).slice(before)) {
      const input = argsOf(call.arguments);
      // The kernel journals a search it refused before the tool ran too, which the tool never answered.
      const mine = this.answered[at]?.key === JSON.stringify(input) ? this.answered[at++] : undefined;
      const text = mine?.output ?? call.result;
      this.d.tools.note(call.ref, input, mine?.ok ? mine.output : "", !neverRan(call));
      req.trace.write("query", { toolCallId: call.ref, tool: call.tool, input });
      const traceId = req.trace.write("tool-output", { toolCallId: call.ref, ok: call.ok, output: text });
      out.push({ id: call.ref, tool: call.tool, input, ok: call.ok, traceId, text });
    }
    return out;
  }
}

// Traces the submission.
export function finishTurn(req: TurnRequest, submission: unknown) {
  if (submission !== null) req.trace.write("submission", { tool: req.submitTool, submission });
}

// Replays recorded or hand-written turns, matched by (move, correlationId) in order. Tool calls run for real
// through the registry (a cassette backend in tests), so refs and traces match a live run.
export class ScriptedModelClient implements ModelClient {
  private used = new Set<number>();
  constructor(private d: ClientDeps, private script: ScriptEntry[]) {}
  get remaining() { return this.script.length - this.used.size; }

  async runTurn(req: TurnRequest): Promise<TurnResult> {
    const i = this.script.findIndex((s, j) => !this.used.has(j) && s.move === req.move && s.correlationId === req.correlationId);
    if (i < 0) throw new Error(`scripted model: no entry for (${req.move}, ${req.correlationId})`);
    this.used.add(i);
    const s = this.script[i]!;
    const toolCalls: ToolCallOut[] = [];
    for (const c of s.toolCalls) toolCalls.push(await runTool(this.d, req, c.tool, c.input));
    const raw = mapIds(s.submission, (x) => (/^\$\d+$/.test(x) ? toolCalls[Number(x.slice(1))]?.id ?? x : x));
    if (raw === null) return { submission: null, toolCalls };
    const submission = clip(raw);
    const bad = schemaErrors(SUBMIT[req.submitTool], submission);
    if (bad) { req.trace.write("rejected-submission", { input: raw, error: bad }); return { submission: null, toolCalls }; }
    req.trace.write("submission", { tool: req.submitTool, submission });
    return { submission, toolCalls };
  }
}
