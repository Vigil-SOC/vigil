import { createHash } from "node:crypto";
import { Ajv, type ValidateFunction } from "ajv";
import OpenAI from "openai";
import type { ChatCompletionMessageParam } from "openai/resources/chat/completions";
import { estimateTokens, Limiter, statusOf } from "./limiter.js";
import type { DecisionProvider, DisconfirmationCritic, WorkerDispatcher } from "./ports.js";
import type { HuntSpec, Rates, RoleSpec } from "./spec.js";
import { toOpenAITools, type Tool } from "./tools.js";
import {
  addTokens,
  NO_TOKENS,
  type Decision,
  type DecisionResult,
  type Digest,
  type DispatchRequest,
  type DispatchResult,
  type NullCheckInput,
  type NullCheckResult,
  type Salience,
  type TokenCounts,
  type ToolCall,
} from "./types.js";

const MAX_TOOL_TURNS = 12;

// Sent on every request because the gateway must supply one to reach Anthropic,
// and its default is small enough to cut a worker off mid-JSON — which arrives
// as an unparseable emission and costs the hunt an iteration rather than looking
// like a limit. Kept under ~16k so a non-streaming call cannot outlive the SDK's
// HTTP timeout; a body that sets its own still wins.
const MAX_OUTPUT_TOKENS = 12_000;

// Enough to hold an aggregate result, not enough for one query to dominate the
// ledger. Capped at capture, where the worker boundary already normalizes.
const MAX_PAYLOAD_CHARS = 8_000;
const MAX_TOOL_RESULT_CHARS = 4_000;

// A snapshot naming "v1" while the prompt underneath it changes is not a
// snapshot. The hash is over what the role was actually told.
export function promptVersion(name: string, role: RoleSpec): string {
  return `${name}/${createHash("sha256").update(role.prompt).digest("hex").slice(0, 12)}`;
}

// Carries what was spent before it failed: a call that died mid-way still burned
// tokens, and dropping the number is how a hunt overruns its budget quietly.
export class LlmError extends Error {
  constructor(
    message: string,
    readonly cost_usd = 0,
    readonly tokens: TokenCounts = NO_TOKENS,
  ) {
    super(message);
  }
}

function truncate(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, limit)}… [truncated from ${text.length} chars]`;
}

export function bifrostUrl(): string {
  return (process.env["BIFROST_URL"] ?? "http://localhost:8080").replace(/\/+$/, "");
}

// Bifrost injects the real provider keys; the client never holds one.
export function createClient(): OpenAI {
  return new OpenAI({ baseURL: `${bifrostUrl()}/v1`, apiKey: "bifrost" });
}

// USD per million tokens. An unknown model costs a visible zero rather than a
// silently misattributed number.
export function costOf(rates: Rates, inputTokens: number, outputTokens: number): number {
  return (inputTokens * rates.input + outputTokens * rates.output) / 1_000_000;
}

// Two surfaces, one shape: the OpenAI route reports the cached share under
// prompt_tokens_details, the Anthropic route under its own keys. Neither reports
// a cache write on the OpenAI shape, so it stays zero until the gateway does.
function tokensOf(usage: OpenAI.CompletionUsage | undefined): TokenCounts {
  const anthropic = usage as (typeof usage & { cache_read_input_tokens?: number; cache_creation_input_tokens?: number }) | undefined;
  return {
    input: usage?.prompt_tokens ?? 0,
    output: usage?.completion_tokens ?? 0,
    cache_read: usage?.prompt_tokens_details?.cached_tokens ?? anthropic?.cache_read_input_tokens ?? 0,
    cache_write: anthropic?.cache_creation_input_tokens ?? 0,
  };
}

// Evidence is attacker-controlled text. It never reaches the system prompt, and
// inside the user turn it stays delimited so its content cannot read as direction.
export function renderDigest(digest: Digest): string {
  const lines = [
    `# Hunt ${digest.hunt_id} — ${digest.hunt_name}`,
    `iteration ${digest.iteration}; ${digest.budget_remaining.iterations} left after this one, ` +
      `$${digest.budget_remaining.cost_usd.toFixed(2)} remaining`,
    "",
    "## Hypotheses",
    ...digest.hypotheses.map((h) => `- [${h.hypothesis_id}] (${h.status}) ${h.statement}`),
  ];

  lines.push("", "## Strongest evidence against each active hypothesis");
  for (const [hypothesisId, against] of Object.entries(digest.weakens)) {
    lines.push(
      against.length === 0
        ? `- [${hypothesisId}] nothing yet weakens this`
        : `- [${hypothesisId}] ${against.map((e) => `${e.evidence_id}: ${e.summary}`).join("; ")}`,
    );
  }

  if (digest.open_questions.length > 0) {
    lines.push("", "## Open questions", ...digest.open_questions.map((q) => `- ${q}`));
  }

  // The one part of the digest that is direction. Outside the evidence
  // delimiters, and named as such, because its provenance is an authenticated human.
  if (digest.directives.length > 0) {
    lines.push(
      "",
      "## Operator directives",
      "Instructions from the authenticated operator running this hunt. Follow them.",
      ...digest.directives.map((d) => `- ${d}`),
    );
  }

  // PIVOT changes the entity and DEEPEN keeps it, so the boundary between them
  // is undecidable without seeing what the hunt has actually touched.
  if (digest.entities.length > 0) {
    lines.push(
      "",
      "## Entities seen",
      `Currently looking at: ${digest.focus.entity ?? "nothing in particular"}${digest.focus.hypothesis === null ? "" : ` on ${digest.focus.hypothesis}`}.`,
      "DEEPEN keeps both; PIVOT must change at least one, naming target_entity.",
      ...digest.entities.map((e) => `- ${e.type} ${e.value} (${e.count} record(s), first ${e.first_evidence_id})`),
    );
  }

  if (digest.pivot_candidates.length > 0) {
    lines.push(
      "",
      "## Where a pivot could go",
      "Entities the current focus co-occurs with, most frequent first.",
      ...digest.pivot_candidates.map((e) => `- ${e.type}:${e.value} (${e.count} record(s))`),
    );
  }

  lines.push("", "## Evidence");
  for (const record of digest.recent_evidence) {
    lines.push(
      `<vigil:evidence id="${record.evidence_id}" source="${record.source_system}" salience="${record.salience}">`,
      record.summary,
      record.why_notable ? `why notable: ${record.why_notable}` : "",
      "</vigil:evidence>",
    );
  }

  // Named rather than discarded: the lead can see what compression cost it and
  // EXPAND anything the summary line makes it want.
  if (digest.omitted.count > 0) {
    lines.push(
      "",
      "## Compressed",
      `${digest.omitted.count} routine record(s) are not shown: ${digest.omitted.evidence_ids.join(", ")}.`,
      "EXPAND any of these ids to read it in full.",
    );
  }

  // Raw telemetry, so delimited exactly like the summaries it came from.
  if (digest.expansions.length > 0) {
    lines.push("", "## Expanded payloads");
    for (const expansion of digest.expansions) {
      lines.push(
        `<vigil:evidence id="${expansion.evidence_id}" source="raw payload" salience="expanded">`,
        expansion.payload,
        "</vigil:evidence>",
      );
    }
  }

  if (digest.notes.length > 0) lines.push("", "## Notes", ...digest.notes.map((n) => `- ${n}`));
  return lines.filter((line) => line !== "").join("\n");
}

// The critic reads raw payloads, delimited the same way: the case against a
// hypothesis is built from what was collected, not from the digest's summary of it.
export function renderNullCheck(check: NullCheckInput): string {
  const lines = [
    "# Hypothesis put up for a verdict",
    `[${check.hypothesis_id}] ${check.statement}`,
    "",
    "## Everything the hunt has linked to it",
  ];

  if (check.evidence.length === 0) lines.push("Nothing is linked to this hypothesis.");
  for (const { relation, record } of check.evidence) {
    lines.push(
      `<vigil:evidence id="${record.evidence_id}" relation="${relation}" source="${record.source_system}" ` +
        `attacker_influenceable="${record.attacker_influenceable}">`,
      record.summary,
      record.why_notable ? `why notable: ${record.why_notable}` : "",
      JSON.stringify(record.payload),
      "</vigil:evidence>",
    );
  }

  if (check.narrative) lines.push("", "## Scenario", check.narrative);
  return lines.filter((line) => line !== "").join("\n");
}

export function renderDispatch(request: DispatchRequest, narrative: string): string {
  const lines = [`# Query intent`, request.query_intent];
  if (request.focus) lines.push("", "## Your focus", request.focus);
  if (request.target_hypothesis_id !== null) {
    lines.push("", `This bears on hypothesis ${request.target_hypothesis_id}.`);
  }
  if (Object.keys(request.scope).length > 0) {
    lines.push("", "## Scope", JSON.stringify(request.scope));
  }
  if (narrative) lines.push("", "## Scenario", narrative);
  return lines.join("\n");
}

// Stable prefix first so a provider that caches prompts can reuse it across iterations.
export function input(role: RoleSpec, body: string): ChatCompletionMessageParam[] {
  return [
    { role: "system", content: role.prompt },
    { role: "user", content: body },
  ];
}

export function output_schema(role: RoleSpec): Record<string, unknown> {
  return role.output_schema;
}

export function toolsFor(role: RoleSpec, tools: readonly Tool[]): Tool[] {
  return tools.filter((tool) => role.tools.includes(tool.id));
}

export interface LlmResult<T> {
  value: T;
  model: string;
  cost_usd: number;
  tokens: TokenCounts;
  rejected: string[];
  // Every tool invocation and what it returned: the raw substrate behind the
  // role's answer, so the ledger holds the data and not only the prose.
  calls: ToolCall[];
}

interface LlmOptions {
  client: OpenAI;
  model: string;
  messages: ChatCompletionMessageParam[];
  schema: Record<string, unknown>;
  tools?: readonly Tool[];
  limiter: Limiter;
  rates: Rates;
  // Cancels the call in flight. A halted hunt should stop paying for a query
  // whose answer it will never read.
  signal?: AbortSignal;
}

// Two stages on purpose: a free-form tool loop, then a separate schema-constrained
// emit. Combining tools with a strict response_format degrades unpredictably
// across the providers Bifrost fronts, and a silent schema violation is the worst
// failure mode available here.
export async function llm_output<T>(options: LlmOptions): Promise<LlmResult<T>> {
  const { client, model, schema, limiter, rates } = options;
  const tools = options.tools ?? [];
  const messages = [...options.messages];
  const executed: ToolCall[] = [];
  let cost = 0;
  let tokens = NO_TOKENS;

  const call = async (body: Parameters<typeof client.chat.completions.create>[0]) => {
    // Before the limiter, not only inside the request: a call still queued
    // behind a rate limit is the cheapest one to give up on.
    options.signal?.throwIfAborted();
    const estimate = estimateTokens(JSON.stringify(body));
    const response = await limiter.run(estimate, () =>
      client.chat.completions.create(
        { max_tokens: MAX_OUTPUT_TOKENS, ...body },
        options.signal ? { signal: options.signal } : {},
      ),
    );
    if (!("choices" in response)) throw new LlmError("streaming responses are not supported", cost, tokens);
    tokens = addTokens(tokens, tokensOf(response.usage));
    cost += costOf(rates, response.usage?.prompt_tokens ?? 0, response.usage?.completion_tokens ?? 0);
    return response;
  };

  for (let turn = 0; turn < MAX_TOOL_TURNS && tools.length > 0; turn += 1) {
    const response = await call({ model, messages, tools: toOpenAITools(tools) });
    const message = response.choices[0]?.message;
    if (message === undefined) throw new LlmError("model returned no message", cost, tokens);

    const calls = message.tool_calls ?? [];
    if (calls.length === 0) break;

    messages.push(message);
    for (const toolCall of calls) {
      if (toolCall.type !== "function") continue;
      const tool = tools.find((candidate) => candidate.id === toolCall.function.name);
      const content = await runTool(tool, toolCall.function.arguments);
      executed.push({
        tool: toolCall.function.name,
        arguments: toolCall.function.arguments,
        result: truncate(content, MAX_TOOL_RESULT_CHARS),
      });
      messages.push({ role: "tool", tool_call_id: toolCall.id, content });
    }
  }

  const rejected: string[] = [];
  const validate = compile(schema);

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const content = await emitJson(call, model, [
      ...messages,
      { role: "user", content: "Emit your decision now as JSON matching the schema." },
    ], schema);
    const parsed = tryParse(content);
    if (parsed !== undefined && validate(parsed)) {
      return { value: parsed as T, model, cost_usd: cost, tokens, rejected, calls: executed };
    }

    const reason = parsed === undefined ? "response was not valid JSON" : formatErrors(validate);
    rejected.push(`${reason}: ${content.slice(0, 400)}`);
    // The rejected emission goes back as the assistant turn it was. Without it the
    // model is asked to correct something it cannot see, and the re-ask lands as a
    // second consecutive user turn — which is how one bad emission became three.
    messages.push({ role: "assistant", content });
    messages.push({ role: "user", content: `That emission was rejected — ${reason}. Emit a valid decision.` });
  }

  throw new LlmError(`model never emitted a valid decision: ${rejected.join(" | ")}`, cost, tokens);
}

export const EMIT_TOOL = "emit_decision";

// Not every provider Bifrost fronts honours response_format. A tool whose
// parameters are the schema works everywhere, so a 400 downgrades to it once
// and the process remembers rather than probing on every call. Remembered per
// model: response_format is a property of the provider behind one model name,
// not of this process, and one gateway's 400 must not downgrade every other.
const emitModes = new Map<string, "schema" | "tool">();

export function resetEmitMode(): void {
  emitModes.clear();
}

type Call = (body: OpenAI.Chat.ChatCompletionCreateParamsNonStreaming) => Promise<OpenAI.Chat.ChatCompletion>;

async function emitJson(
  call: Call,
  model: string,
  messages: ChatCompletionMessageParam[],
  schema: Record<string, unknown>,
): Promise<string> {
  if ((emitModes.get(model) ?? "schema") === "schema") {
    try {
      const response = await call({
        model,
        messages,
        response_format: { type: "json_schema", json_schema: { name: "decision", strict: false, schema } },
      });
      return textOf(response.choices[0]?.message?.content);
    } catch (error) {
      if (statusOf(error) !== 400) throw error;
      emitModes.set(model, "tool");
    }
  }

  const response = await call({
    model,
    messages,
    tools: [{ type: "function", function: { name: EMIT_TOOL, description: "Emit the decision.", parameters: schema } }],
    tool_choice: { type: "function", function: { name: EMIT_TOOL } },
  });
  const toolCall = response.choices[0]?.message?.tool_calls?.[0];
  return toolCall?.type === "function" ? toolCall.function.arguments : "";
}

// The gateway fronts providers whose native reply is a content-block list, and
// that shape reaches us intact often enough to matter. Handing an array to
// JSON.parse stringifies it to [object Object], so the emission reads as invalid
// JSON and a decision the model got right is thrown away.
function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block) => (typeof block === "object" && block !== null ? String((block as { text?: unknown }).text ?? "") : ""))
    .join("");
}

async function runTool(tool: Tool | undefined, rawArgs: string): Promise<string> {
  if (tool === undefined) return "no such tool";
  try {
    return await tool.run(JSON.parse(rawArgs) as Record<string, unknown>);
  } catch (error) {
    // A tool failure is evidence about visibility; the loop keeps going.
    return `tool failed: ${(error as Error).message}`;
  }
}

function tryParse(content: string): unknown {
  try {
    return JSON.parse(content);
  } catch {
    return undefined;
  }
}

const ajv = new Ajv({ allErrors: true, strict: false });
const compiled = new Map<string, ValidateFunction>();

function compile(schema: Record<string, unknown>): ValidateFunction {
  const key = JSON.stringify(schema);
  const existing = compiled.get(key);
  if (existing !== undefined) return existing;
  const validate = ajv.compile(schema);
  compiled.set(key, validate);
  return validate;
}

function formatErrors(validate: ValidateFunction): string {
  return (validate.errors ?? []).map((error) => `${error.instancePath || "/"} ${error.message}`).join("; ");
}

// One limiter shared by both roles: the rate limit belongs to the gateway, not
// to any single caller of it.
export function createLimiter(spec: HuntSpec): Limiter {
  return new Limiter(spec.runtime.rate_limit, spec.runtime.concurrency, spec.runtime.retry_attempts);
}

export class LlmDecisionProvider implements DecisionProvider {
  private readonly role: RoleSpec;
  private readonly tools: Tool[];

  constructor(
    private readonly spec: HuntSpec,
    tools: readonly Tool[] = [],
    private readonly limiter: Limiter = createLimiter(spec),
    private readonly client: OpenAI = createClient(),
  ) {
    this.role = spec.roles.lead;
    this.tools = toolsFor(this.role, tools);
  }

  async decide(digest: Digest): Promise<DecisionResult> {
    const body = [renderDigest(digest), digest.narrative ? `\n## Scenario\n${digest.narrative}` : ""]
      .join("\n")
      .trim();
    const result = await llm_output<Decision>({
      client: this.client,
      model: this.spec.model,
      messages: input(this.role, body),
      schema: output_schema(this.role),
      tools: this.tools,
      limiter: this.limiter,
      rates: this.spec.rates,
    });

    return {
      decision: result.value,
      model_id: result.model,
      prompt_version: promptVersion("lead", this.role),
      cost_usd: result.cost_usd,
      tokens: result.tokens,
      rejected_attempts: result.rejected,
    };
  }
}

// The critic is never asked whether the hypothesis is true — only whether the
// benign story it just built accounts for the evidence. Asking it to rate the
// hypothesis directly makes it a second Hunt Lead, which is the bias this whole
// pass exists to cancel.
interface CriticOutput {
  benign_explanation: string;
  benign_explanation_stands: boolean;
  rationale: string;
}

export class LlmDisconfirmationCritic implements DisconfirmationCritic {
  private readonly role: RoleSpec;
  private readonly tools: Tool[];

  constructor(
    private readonly spec: HuntSpec,
    tools: readonly Tool[] = [],
    private readonly limiter: Limiter = createLimiter(spec),
    private readonly client: OpenAI = createClient(),
  ) {
    const role = spec.roles.critic;
    if (role === undefined) throw new LlmError("this arch declares no critic role");
    this.role = role;
    this.tools = toolsFor(role, tools);
  }

  async argueNull(check: NullCheckInput): Promise<NullCheckResult> {
    const result = await llm_output<CriticOutput>({
      client: this.client,
      model: this.spec.model,
      messages: input(this.role, renderNullCheck(check)),
      schema: output_schema(this.role),
      tools: this.tools,
      limiter: this.limiter,
      rates: this.spec.rates,
    });

    return {
      survives: !result.value.benign_explanation_stands,
      strongest_benign_explanation: result.value.benign_explanation,
      rationale: result.value.rationale,
      cost_usd: result.cost_usd,
      tokens: result.tokens,
      model_id: result.model,
      prompt_version: promptVersion("critic", this.role),
    };
  }
}

// An arch with no critic role gets no critic rather than a startup failure: the
// hunt still runs, it just cannot prove anything and says so each VALIDATE.
export function criticFor(
  spec: HuntSpec,
  tools: readonly Tool[] = [],
  limiter?: Limiter,
  client?: OpenAI,
): DisconfirmationCritic | undefined {
  if (spec.roles.critic === undefined) return undefined;
  return new LlmDisconfirmationCritic(spec, tools, limiter, client);
}

interface WorkerOutput {
  results: {
    // The telemetry plane the finding came out of, not the worker that ran it:
    // corroboration means two systems agreeing, and one agent querying twice is
    // one system. Narrowed to the playbook's declared domains at spec build.
    source_system: string;
    summary: string;
    salience: Salience;
    why_notable: string;
    // The rows or aggregates the claim rests on. Without it the critic argues
    // against the worker's prose, which is the framing it exists to escape.
    payload?: Record<string, unknown>;
    supports?: string[];
    weakens?: string[];
    attacker_influenceable?: boolean;
  }[];
  ips_to_check?: string[];
}

function cappedPayload(payload: Record<string, unknown> | undefined): Record<string, unknown> {
  if (payload === undefined) return {};
  const json = JSON.stringify(payload);
  if (json.length <= MAX_PAYLOAD_CHARS) return payload;
  return { truncated: true, preview: truncate(json, MAX_PAYLOAD_CHARS) };
}

export class LlmWorkerDispatcher implements WorkerDispatcher {
  constructor(
    private readonly spec: HuntSpec,
    private readonly tools: readonly Tool[] = [],
    private readonly limiter: Limiter = createLimiter(spec),
    private readonly client: OpenAI = createClient(),
  ) {}

  // Resolved per request, not in the constructor: which specialist runs is the
  // Hunt Lead's decision, and each one carries its own prompt and tool scope.
  async dispatch(request: DispatchRequest): Promise<DispatchResult> {
    const role = this.spec.roles.workers[request.agent_id];
    if (role === undefined) throw new LlmError(`no such worker ${request.agent_id}`);

    const result = await llm_output<WorkerOutput>({
      client: this.client,
      model: this.spec.model,
      messages: input(role, renderDispatch(request, this.spec.narrative)),
      schema: output_schema(role),
      tools: toolsFor(role, this.tools),
      limiter: this.limiter,
      rates: this.spec.rates,
      ...(request.signal ? { signal: request.signal } : {}),
    });

    return {
      dispatch_id: request.dispatch_id,
      evidence: result.value.results.map((item) => ({
        source_system: item.source_system,
        summary: item.summary,
        payload: cappedPayload(item.payload),
        salience: item.salience,
        why_notable: item.why_notable,
        provenance: "worker",
        attacker_influenceable: item.attacker_influenceable ?? false,
        instruction_like: false,
        supports: item.supports ?? [],
        weakens: item.weakens ?? [],
      })),
      questions: (result.value.ips_to_check ?? []).map((ip) => `check ${ip}`),
      failed: false,
      failure_reason: "",
      cost_usd: result.cost_usd,
      tokens: result.tokens,
      calls: result.calls,
    };
  }
}
