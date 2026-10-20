// Model calls (spec §5.2). A move is one kernel turn: the model searches up to the move's cap through the evidence
// tool, then emits the move's submission against its schema. The kernel journals every search as a `call` event
// with a ref, bills the model, delimits the results and resumes; what is left here is the move's own check, once.
import { runSubturn, type PluginContext, type PluginHooks, type Subturn, type AgentKinds, type AgentOptions } from "../../../../core/agent.js";
import type { CallPayload, RunKind } from "../../../../contracts/events.js";
import { defineTool, type RegisteredTool, type ToolResult } from "../../../../contracts/tool.js";
import type { Harness, Outcome } from "../../../../core/loop.js";
import type { PluginManifest } from "../../../../contracts/plugin.js";
import type { RunSpec } from "../../../../core/spec.js";
import type { AgentConfig } from "../config.js";
import type { Move } from "../frontier.js";
import { issued, type EvidenceTool, type Registry, type ToolRegistry } from "../tools/registry.js";
import type { TurnTrace } from "../trace.js";
import type { Domain } from "../types.js";
import { SUBMIT, SUBMIT_DESCRIPTIONS, jsonSchema, type SubmitName } from "./schemas.js";
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

// Over-long text is trimmed, not refused: a long note must not throw away a correct finding.
const LIMITS: Record<string, number> = { conclusion: 2500, map: 12000, summary: 300, label: 160, reason: 300, reasoning: 800, rootJustification: 400, story: 700, expect: 300, gaps: 300 };
// A list or object sent as a JSON string (models do send events that way) is parsed, not refused.
const STRUCT = new Set(["takeover", "candidates", "effects", "causes", "events", "evidence", "nodeEvidence", "edgeEvidence", "artifacts", "identitySignals", "baseline", "boundary", "theories", "predictions", "nextTests", "toolCallIds", "supports", "weakens", "links", "unrelated", "contradicts"]);
const unstring = (v: string): unknown => { try { const x = JSON.parse(v); return x && typeof x === "object" ? x : v; } catch { return v; } };
const clip = (v: unknown, key = ""): unknown =>
  typeof v === "string" && STRUCT.has(key) && /^\s*[[{]/.test(v) && unstring(v) !== v ? clip(unstring(v), key)
  : typeof v === "string" ? (LIMITS[key] && v.length > LIMITS[key] ? v.slice(0, LIMITS[key] - 1) + "…" : v)
  : Array.isArray(v) ? v.map((x) => (typeof x === "string" && key === "suspiciousContent" ? x.slice(0, 300) : clip(x)))
  : v && typeof v === "object" ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, clip(x, k)])) : v;

export const validate = (name: SubmitName, input: unknown) => SUBMIT[name].safeParse(clip(input));

// Schema, then citations: every toolCallId must be a search the run journaled. A failure is not sent back to the
// model: salvage keeps what is sound and the move's attempt is spent.
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
// drop: the list items (key, index) a failure rests on, so a last salvage can keep the rest of the submission.
export type CheckResult = { success: true; data: unknown } | { success: false; msg: string; drop: [string, number][] };

export function check(name: SubmitName, input: unknown, callCount: number, cx: CheckCtx = {}): CheckResult {
  const v = validate(name, input);
  if (!v.success) {
    const drop = v.error.issues.map((i) => i.path).filter((p) => typeof p[0] === "string" && typeof p[1] === "number" && p.length >= 2).map((p) => [p[0], p[1]] as [string, number]);
    return { success: false, msg: v.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; "), drop: drop.length === v.error.issues.length ? drop : [] };
  }
  // A list item citing a ref the run never issued fails the check and salvage drops it, the rest kept; a bad
  // citation outside any list fails the submission whole.
  const unissued = (x: unknown): string[] => Array.isArray(x) ? x.flatMap(unissued) : x && typeof x === "object" ? Object.entries(x).flatMap(([k, y]) => (k === "toolCallIds" && Array.isArray(y) ? y.map(String).filter((id) => !issued({ callCount }, id)) : unissued(y))) : [];
  const top = Object.entries(v.data as Record<string, unknown>);
  const dropped = top.flatMap(([k, x]) => (Array.isArray(x) ? x.flatMap((item, i) => (unissued(item).length ? [[k, i] as [string, number]] : [])) : []));
  const loose = top.flatMap(([, x]) => (Array.isArray(x) ? [] : unissued(x)));
  if (dropped.length || loose.length) {
    const ids = new Set([...loose, ...dropped.flatMap(([k, i]) => unissued((v.data as unknown as Record<string, unknown[]>)[k]![i]))]);
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


// A failed check drops the list items it rests on and keeps the rest, so one bad event does not throw away
// the others (a run once lost a correct finding with its whole submission).
export function salvage(name: SubmitName, input: unknown, callCount: number, cx: CheckCtx, first: CheckResult): { data: unknown; dropped: number } | null {
  let cur = clip(input) as Record<string, unknown[]>, r = first, dropped = 0;
  for (let i = 0; i < 3 && !r.success; i++) {
    if (!r.drop.length) return null;
    const gone = new Set(r.drop.map(([k, j]) => `${k}.${j}`));
    cur = Object.fromEntries(Object.entries(cur).map(([k, x]) => [k, Array.isArray(x) ? x.filter((_, j) => !gone.has(`${k}.${j}`)) : x]));
    dropped += gone.size;
    r = check(name, cur, callCount, cx);
  }
  return r.success ? { data: r.data, dropped } : null;
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
  return { description: SUBMIT_DESCRIPTIONS[name], ...(strip(jsonSchema(SUBMIT[name])) as Record<string, unknown>) };
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
        parameters: jsonSchema(t.inputSchema),
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
    let submission: unknown = null;
    if (outcome!.value === null) req.trace.write("rejected-submission", { reason: outcome!.reason });
    else {
      const cx: CheckCtx = { inputOf: tools.inputOf?.bind(tools), range: this.d.range };
      const v = check(req.submitTool, outcome!.value, tools.callCount, cx);
      if (v.success) submission = v.data;
      else {
        const s = salvage(req.submitTool, outcome!.value, tools.callCount, cx, v);
        req.trace.write("rejected-submission", { input: outcome!.value, error: v.msg, ...(s ? { salvaged: `kept the rest, dropped ${s.dropped} item(s)` } : {}) });
        submission = s?.data ?? null;
      }
    }
    finishTurn(req, submission);
    return { submission, toolCalls };
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
      this.d.tools.note(call.ref, input, mine?.ok ? mine.output : "");
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
    const v = validate(req.submitTool, raw);
    if (!v.success) { req.trace.write("rejected-submission", { input: raw, error: v.error.message }); return { submission: null, toolCalls }; }
    req.trace.write("submission", { tool: req.submitTool, submission: v.data });
    return { submission: v.data, toolCalls };
  }
}
