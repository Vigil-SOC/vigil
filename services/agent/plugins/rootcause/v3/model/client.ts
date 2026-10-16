// Model calls (spec §5.2). Each handler call is a bounded sub-loop: the model may search up to
// maxToolCallsPerTurn times, then must call the submit tool the move names. The submission is validated with
// zod; one correction round on a validation error, one re-prompt if it stops without submitting.
// Tools and system prompt are identical on every call (all four submit tools are always offered), so the
// cached prefix survives across moves.
import type { MessageParam, MessageRequest, Messages, TextBlock, Tool, ToolResultBlock, ToolUseBlock } from "./messages.js";
import type { AgentConfig } from "../config.js";
import type { Budget, Usage } from "../budget.js";
import type { Move } from "../frontier.js";
import type { ToolRegistry } from "../tools/registry.js";
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
  model?: string; // a different (cheaper) model for this turn
  minCount?: number; // mapping mode for the search tool
  maxPerBatch?: number; // searches run per model response; the rest wait for the results
  priceScale?: number; // its price relative to cfg.model
  trace: TurnTrace;
}

export interface ToolCallOut { id: string; tool: string; input: unknown; ok: boolean; traceId: string; text?: string }

export interface TurnResult {
  submission: unknown | null;
  toolCalls: ToolCallOut[];
  usage: Usage;
}

export interface ModelClient {
  runTurn(req: TurnRequest): Promise<TurnResult>;
  close?(exit: string): void;
  addContext?(text: string): void; // appended to the system prompt (the environment map), cached with it
}

export interface ClientDeps { cfg: AgentConfig; tools: ToolRegistry; budget: Budget; domains: Domain[]; survey: string; range: [string, string] }

// Runs one evidence tool call and traces it: the query, then the full untruncated output.
export async function runTool(d: ClientDeps, req: TurnRequest, tool: string, input: unknown): Promise<{ out: ToolCallOut; forModel: string }> {
  const rec = await d.tools.call(tool, input, { allowedDomains: req.allowedDomains, allDomains: d.domains, timeoutMs: d.cfg.toolTimeoutMs, range: d.range, focusTime: req.focusTime, beforeMs: req.beforeMs, minCount: req.minCount });
  req.trace.write("query", { toolCallId: rec.toolCallId, tool, input });
  const traceId = req.trace.write("tool-output", { toolCallId: rec.toolCallId, ok: rec.result.ok, output: rec.result.ok ? rec.result.data : rec.result.error });
  const max = d.cfg.rawOutputMaxChars;
  const forModel = rec.wrapped.length > max ? rec.wrapped.slice(0, max) + `\n…[cut at ${max} chars: aggregate or narrow the search]\n</tool_output>` : rec.wrapped;
  return { out: { id: rec.toolCallId, tool, input, ok: rec.result.ok, traceId, text: rec.result.ok ? rec.result.data : rec.result.error }, forModel };
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

// Schema, then citations: every toolCallId must be a search issued in this run. A failure goes back to the
// model to fix, instead of the evidence being dropped silently later.
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
export interface CheckCtx { inputOf?: InputOf; range?: [string, string]; loose?: boolean }
// drop: the list items (key, index) a failure rests on, so a last salvage can keep the rest of the submission.
export type CheckResult = { success: true; data: unknown } | { success: false; msg: string; drop: [string, number][] };

export function check(name: SubmitName, input: unknown, callCount: number, cx: CheckCtx = {}): CheckResult {
  const v = validate(name, input);
  if (!v.success) {
    const drop = v.error.issues.map((i) => i.path).filter((p) => typeof p[0] === "string" && typeof p[1] === "number" && p.length >= 2).map((p) => [p[0], p[1]] as [string, number]);
    return { success: false, msg: v.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; "), drop: drop.length === v.error.issues.length ? drop : [] };
  }
  const bad = new Set<string>();
  const walk = (x: unknown): void => {
    if (Array.isArray(x)) x.forEach(walk);
    else if (x && typeof x === "object") for (const [k, y] of Object.entries(x)) {
      if (k === "toolCallIds" && Array.isArray(y)) for (const id of y) { const m = /^tc_(\d+)$/.exec(String(id)); if (!m || Number(m[1]) < 1 || Number(m[1]) > callCount) bad.add(String(id)); }
      else walk(y);
    }
  };
  walk(v.data);
  if (bad.size) return { success: false, msg: `toolCallIds never issued in this run: ${[...bad].join(", ")}. Cite only tc_ ids from your searches or from the snapshot.`, drop: [] };
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
  // Loose: an accusing label without a baseline, or a baseline that fails the checks, is kept and tagged
  // unverified (the report never accuses from it; a failed baseline proves nothing either way, so it is dropped).
  if (cx.loose) {
    for (const { c } of accusing) (c as Cand & { unverified?: boolean }).unverified = true;
    for (const { c } of items) if (baselineProblem(c as never, cx.inputOf, cx.range)) { (c as Cand & { unverified?: boolean }).unverified = true; delete c.baseline; }
  }
  if (accusing.length && !cx.loose) return { success: false, drop: accusing.map((x) => x.at), msg: `${accusing.map(({ c }) => `"${c.label.slice(0, 80)}"`).join("; ")}: the label calls this activity malicious or the attacker's but there is no baseline showing it is not that account's or address's normal work. Add a baseline (a cited search of the same account/address over the whole range, unusual: true), or relabel it neutrally (what happened, by which account, from which address).` };
  // An event is a record the searches returned. "Happened outside the logs" is not one: it ends the chain on an
  // absence and stops the search for the real step ("credential minted outside captured
  // telemetry" was once confirmed and became the chain top; the token theft upstream was never looked for).
  const unseen = items.filter(({ c }) => UNSEEN.test(c.label));
  if (unseen.length) return { success: false, drop: unseen.map((x) => x.at), msg: `${unseen.map(({ c }) => `"${c.label.slice(0, 80)}"`).join("; ")}: an event must be a record your searches returned, not something inferred from records that are missing. Leave it out and report the records that do exist: earlier records naming the same credential, key, token, session, file, host or pod (where it was read, written, copied or used first). If nothing in the data shows the cause, report no candidate for it (or a boundary to the source that would record it).` };
  // Every baseline, accusing or clearing, must be the event's own account/host over the whole range.
  const weak = items.map((x) => ({ ...x, why: baselineProblem(x.c as never, cx.inputOf, cx.range) })).filter((x) => x.why);
  if (weak.length) return { success: false, drop: weak.map((x) => x.at), msg: weak.map((x) => x.why).join(" ") };
  return { success: true, data: v.data };
}

// Last chance after the corrections: drop the list items the failure rests on and keep the rest, so one bad
// event does not throw away the others (a run once lost a correct finding with its whole submission).
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

export class AnthropicModelClient implements ModelClient {
  private tools: Tool[];
  private system: TextBlock[];
  // client carries each call to the model; nothing here knows which gateway or provider.
  constructor(private d: ClientDeps, private client: Messages) {
    const t = d.tools.list();
    this.tools = [
      ...t.map((x) => ({ name: x.name, description: x.description, input_schema: jsonSchema(x.inputSchema) as Record<string, unknown> })),
      ...(Object.keys(SUBMIT) as SubmitName[]).map((n) => ({ name: n, description: SUBMIT_DESCRIPTIONS[n], input_schema: jsonSchema(SUBMIT[n]) as Record<string, unknown> })),
    ];
    this.system = [{ type: "text", text: SYSTEM_PROMPT }, { type: "text", text: d.survey, cache_control: { type: "ephemeral" } }];
  }

  addContext(text: string) {
    this.system = [this.system[0], { ...this.system[1], text: `${this.system[1].text}\n\n${text}` }];
  }

  // A refusal (the safety filter misfiring on attack narrative built up in the snapshot) is retried: once without
  // the STORIES sections, then once on the fallback model. Searches already run in the refused attempt stay
  // cited-able (their ids are issued) but the retry runs its own.
  async runTurn(req: TurnRequest): Promise<TurnResult> {
    const first = await this.runTurnOnce(req);
    if (!first.refused) return first;
    req.trace.write("refusal", { attempt: 1, model: req.model ?? this.d.cfg.model, retry: "without STORIES" });
    const lean = { ...req, userContent: req.userContent.replace(/\nSTORIES \(competing[\s\S]*?(?=\n<\/graph_snapshot>)/, "") };
    const second = await this.runTurnOnce(lean);
    if (!second.refused) return second;
    req.trace.write("refusal", { attempt: 2, model: lean.model ?? this.d.cfg.model, retry: `fallback model ${this.d.cfg.refusalFallbackModel}` });
    const third = await this.runTurnOnce({ ...req, model: this.d.cfg.refusalFallbackModel, priceScale: this.d.cfg.scopePriceScale });
    if (third.refused) { req.trace.write("refusal", { attempt: 3, gaveUp: true }); return this.done(req, [], null, third.usage); }
    return third;
  }

  private async runTurnOnce(req: TurnRequest): Promise<TurnResult & { refused?: boolean }> {
    const { cfg, budget } = this.d;
    const usage: Usage = { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0 };
    const messages: MessageParam[] = [{ role: "user", content: req.userContent }];
    const toolCalls: ToolCallOut[] = [];
    let searches = 0, nudged = false, corrections = 0;
    const cx: CheckCtx = { inputOf: this.d.tools.inputOf?.bind(this.d.tools), range: this.d.range, loose: cfg.loose };

    const ask = async (force?: SubmitName) => {
      const sent = messages.map((m, i) => i < messages.length - 1 ? m : { ...m, content: (typeof m.content === "string" ? [{ type: "text" as const, text: m.content }] : m.content).map((c, j, a) => j === a.length - 1 ? { ...c, cache_control: { type: "ephemeral" as const } } : c) });
      const msg = await this.client.create({
        model: req.model ?? cfg.model,
        max_tokens: 16000,
        system: this.system,
        tools: this.tools,
        messages: sent as MessageParam[],
        // Forcing a tool is not allowed with thinking on, so a forced turn runs without it.
        ...(force ? { tool_choice: { type: "tool", name: force } } : { thinking: { type: "adaptive", display: "summarized" } }),
      } as MessageRequest);
      const u = msg.usage;
      const turnUsage = { inputTokens: u.input_tokens, outputTokens: u.output_tokens, cacheReadTokens: u.cache_read_input_tokens ?? 0, cacheWriteTokens: u.cache_creation_input_tokens ?? 0 };
      for (const k of Object.keys(usage) as (keyof Usage)[]) usage[k] += turnUsage[k];
      const usd = budget.add(turnUsage, req.priceScale);
      req.trace.write("usage", { ...turnUsage, usd, stopReason: msg.stop_reason });
      const thought = msg.content.flatMap((c) => c.type === "thinking" && c.thinking ? [c.thinking] : c.type === "text" && c.text.trim() ? [c.text] : []).join("\n");
      if (thought) req.trace.write("reasoning", { text: thought });
      messages.push({ role: "assistant", content: msg.content });
      return msg;
    };

    let force: SubmitName | undefined;
    const cap = req.maxSearches ?? cfg.maxToolCallsPerTurn;
    for (let round = 0; round < cap + 6; round++) {
      const msg = await ask(force);
      if (msg.stop_reason === "refusal") return { submission: null, toolCalls, usage, refused: true };
      const calls = msg.content.filter((c): c is ToolUseBlock => c.type === "tool_use");
      const submit = calls.find((c) => c.name === req.submitTool);
      if (submit) {
        const v = check(req.submitTool, submit.input, this.d.tools.callCount, cx);
        if (v.success) return this.done(req, toolCalls, v.data, usage);
        if (corrections >= 2) {
          const s = salvage(req.submitTool, submit.input, this.d.tools.callCount, cx, v);
          req.trace.write("rejected-submission", { input: submit.input, error: v.msg, ...(s ? { salvaged: `kept the rest, dropped ${s.dropped} item(s)` } : {}) });
          return this.done(req, toolCalls, s?.data ?? null, usage);
        }
        corrections++;
      }
      if (!calls.length) {
        if (nudged) return this.done(req, toolCalls, null, usage);
        nudged = true;
        force = req.submitTool;
        messages.push({ role: "user", content: `Submit your findings using ${req.submitTool}.` });
        continue;
      }
      const results: (ToolResultBlock | TextBlock)[] = [];
      let batch = 0;
      for (const c of calls) {
        if (c === submit) {
          const v = check(req.submitTool, c.input, this.d.tools.callCount, cx);
          results.push({ type: "tool_result", tool_use_id: c.id, is_error: true, content: `Invalid submission: ${v.success ? "" : v.msg}. Fix it and call ${req.submitTool} again.` });
        } else if (c.name in SUBMIT) {
          results.push({ type: "tool_result", tool_use_id: c.id, is_error: true, content: `This move reports with ${req.submitTool}, not ${c.name}.` });
        } else if (searches >= cap) {
          results.push({ type: "tool_result", tool_use_id: c.id, is_error: true, content: `Not run: no searches left this turn. Submit now with ${req.submitTool}.` });
        } else if (req.maxPerBatch && batch >= req.maxPerBatch) {
          results.push({ type: "tool_result", tool_use_id: c.id, is_error: true, content: `Not run: at most ${req.maxPerBatch} searches per batch. Read these results first, then send the next batch (fix any search that returned nothing).` });
        } else {
          batch++;
          searches++;
          const { out, forModel } = await runTool(this.d, req, c.name, c.input);
          toolCalls.push(out);
          results.push({ type: "tool_result", tool_use_id: c.id, content: forModel, ...(out.ok ? {} : { is_error: true }) });
        }
      }
      if (searches >= cap) {
        force = req.submitTool;
        results.push({ type: "text", text: `No searches left this turn. Submit now with ${req.submitTool}.` });
      }
      messages.push({ role: "user", content: results });
    }
    return this.done(req, toolCalls, null, usage);
  }

  private done(req: TurnRequest, toolCalls: ToolCallOut[], submission: unknown, usage: Usage): TurnResult {
    finishTurn(req, submission);
    return { submission, toolCalls, usage };
  }
}

// Traces the submission.
export function finishTurn(req: TurnRequest, submission: unknown) {
  if (submission !== null) req.trace.write("submission", { tool: req.submitTool, submission });
}

// Replays recorded or hand-written turns, matched by (move, correlationId) in order. Tool calls run for real
// through the registry (a cassette backend in tests), so ids and traces match a live run.
export class ScriptedModelClient implements ModelClient {
  private used = new Set<number>();
  constructor(private d: ClientDeps, private script: ScriptEntry[]) {}
  get remaining() { return this.script.length - this.used.size; }

  async runTurn(req: TurnRequest): Promise<TurnResult> {
    const i = this.script.findIndex((s, j) => !this.used.has(j) && s.move === req.move && s.correlationId === req.correlationId);
    if (i < 0) throw new Error(`scripted model: no entry for (${req.move}, ${req.correlationId})`);
    this.used.add(i);
    const s = this.script[i];
    const toolCalls: ToolCallOut[] = [];
    for (const c of s.toolCalls) toolCalls.push((await runTool(this.d, req, c.tool, c.input)).out);
    const raw = mapIds(s.submission, (x) => (/^\$\d+$/.test(x) ? toolCalls[Number(x.slice(1))]?.id ?? x : x));
    if (raw === null) return { submission: null, toolCalls, usage: { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0 } };
    const v = validate(req.submitTool, raw);
    if (!v.success) { req.trace.write("rejected-submission", { input: raw, error: v.error.message }); return { submission: null, toolCalls, usage: { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0 } }; }
    req.trace.write("submission", { tool: req.submitTool, submission: v.data });
    return { submission: v.data, toolCalls, usage: { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0 } };
  }
}
