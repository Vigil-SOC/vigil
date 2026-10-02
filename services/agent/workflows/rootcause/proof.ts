import type { DispatchPayload } from "../../contracts/events.js";
import type { BudgetLimits } from "../../contracts/budget.js";
import { seedFrom, type SpentEvent } from "../../core/budget.js";
import type { ToolResult } from "../../contracts/tool.js";

// The only bound id that can express a count before time T. A bare splunk_execute,
// or any other telemetry tool, is not this one.
export const PROVER_TOOL = "splunk-selfhosted_splunk_execute";

export type Proof = "proven" | "rejected" | "unproven" | "none";

export interface Observation {
  tool: string;
  args: string;
  rows: readonly unknown[];
}

export interface StepPayload {
  step_id: string;
  event: string;
  who: string;
  at: string;
  link: string;
  artifact: string;
  cause_id: string | null;
  origin: boolean;
  link_status: Proof;
  origin_status: Proof;
}

export interface NoticePayload {
  text: string;
}

export type RootCauseKinds = { step: StepPayload; notice: NoticePayload };

// A step is proven when its cause link is proven, and, if it claims to be the
// origin, that claim is proven too. A step that claims neither is still open.
export function isProven(step: StepPayload): boolean {
  const cause = step.cause_id !== null && step.link_status === "proven";
  const origin = step.origin && step.origin_status === "proven";
  if (step.origin && step.cause_id !== null) return cause && origin;
  return cause || origin;
}

export function openSteps(steps: readonly StepPayload[]): StepPayload[] {
  return steps.filter((step) => !isProven(step));
}

export function openSummary(steps: readonly StepPayload[]): string {
  const open = openSteps(steps);
  if (steps.length === 0) return "Still unproven: nothing was recorded.";
  if (open.length === 0) return "The ceiling stopped the trace with every recorded step proven.";
  return `Still unproven: ${open.map((step) => `${step.step_id} (${step.event})`).join("; ")}`;
}

// Latest write wins, so a step revised after a failed proof replaces the open one.
export function latestSteps(events: readonly { kind: string; payload: unknown }[]): StepPayload[] {
  const byId = new Map<string, StepPayload>();
  for (const event of events) {
    if (event.kind !== "step") continue;
    const step = event.payload as StepPayload;
    if (typeof step?.step_id === "string") byId.set(step.step_id, step);
  }
  return [...byId.values()];
}

export function observationsOf(events: readonly { kind: string; payload: unknown }[]): Observation[] {
  const found: Observation[] = [];
  for (const event of events) {
    if (event.kind !== "dispatch") continue;
    const payload = event.payload as DispatchPayload;
    const call = Array.isArray(payload.calls) ? payload.calls[0] : undefined;
    if (call === null || typeof call !== "object") continue;
    const record = call as { tool?: unknown; arguments?: unknown };
    const tool = typeof record.tool === "string" ? record.tool : "";
    if (tool === "" || tool === "record" || tool === "finish") continue;
    const result = payload.result as ToolResult | undefined;
    found.push({
      tool,
      args: typeof record.arguments === "string" ? record.arguments : "",
      rows: result?.ok === true ? result.rows : [],
    });
  }
  return found;
}

// The $15 and 90-minute ceilings, off the ledger the same way the pool reads them.
// A park does not count as work. max_calls is not one of these two.
export function ceilingsHit(events: readonly SpentEvent[], limits: BudgetLimits, now = Date.now()): boolean {
  const seed = seedFrom(events, now);
  if (seed.started === 0) return false;
  return seed.spent.cost_usd >= limits.max_cost_usd || now - seed.started >= limits.max_wall_ms;
}

export function proveLink(
  link: string,
  whos: readonly string[],
  causeTime: string,
  observations: readonly Observation[],
): Proof {
  if (link.trim() === "" || causeTime.trim() === "") return "unproven";
  if (notALink(link, whos)) return "rejected";
  const counted = countBefore(link, causeTime, observations);
  if (counted === "rejected") return "rejected";
  if (carriers(observations, link).length < 2) return "unproven";
  return counted;
}

// An origin is the same before-T count, one step further: nothing earlier carries
// the value. The two-event rule does not apply. The starting artifact uses this too.
export function proveOrigin(
  value: string,
  whos: readonly string[],
  at: string,
  observations: readonly Observation[],
): Proof {
  if (value.trim() === "" || at.trim() === "") return "unproven";
  if (notALink(value, whos)) return "rejected";
  return countBefore(value, at, observations);
}

export function originOf(
  link: string,
  artifact: string,
  whos: readonly string[],
  at: string,
  observations: readonly Observation[],
): Proof {
  const values = [link, artifact].filter((value, index, all) => value.trim() !== "" && all.indexOf(value) === index);
  if (values.length === 0) return "unproven";
  const results = values.map((value) => proveOrigin(value, whos, at, observations));
  if (results.includes("proven")) return "proven";
  if (results.every((result) => result === "rejected")) return "rejected";
  return "unproven";
}

// A who stays only when it is the who of a proven step. Every other recorded who
// is replaced, including inside the summary the model wrote.
export function redact(summary: string, steps: readonly StepPayload[]): string {
  const proven = new Set(steps.filter(isProven).map((step) => step.who).filter((who) => who !== ""));
  const hidden = [...new Set(steps.map((step) => step.who).filter((who) => who !== "" && !proven.has(who)))];
  hidden.sort((left, right) => right.length - left.length);
  let text = summary;
  for (const who of hidden) text = text.split(who).join("[unlinked]");
  return text;
}

function notALink(link: string, whos: readonly string[]): boolean {
  if (whos.some((who) => who !== "" && who === link)) return true;
  if (isIp(link)) return true;
  return isTimestamp(link);
}

function carriers(observations: readonly Observation[], link: string): Observation[] {
  return observations.filter((obs) => obs.rows.length > 0 && containsValue(obs.rows, link));
}

// Only the in-repo splunk tool can record this count. Invoke unwraps that tool's
// envelope and journals `results`, so an empty search is no rows — not a count of
// zero — and `| stats count` arrives as the inner row. A count above zero rejects.
function countBefore(link: string, causeTime: string, observations: readonly Observation[]): Proof {
  const counts: number[] = [];
  for (const obs of observations) {
    if (obs.tool !== PROVER_TOOL || obs.rows.length === 0) continue;
    if (!mentions(obs, link) || !mentions(obs, causeTime)) continue;
    counts.push(...countsIn(obs.rows));
  }
  if (counts.some((count) => count > 0)) return "rejected";
  if (counts.some((count) => count === 0)) return "proven";
  return "unproven";
}

function mentions(obs: Observation, needle: string): boolean {
  if (needle === "") return false;
  return obs.args.includes(needle) || containsValue(obs.rows, needle);
}

function countsIn(node: unknown): number[] {
  const found: number[] = [];
  walk(node, found);
  return found;
}

function walk(node: unknown, found: number[]): void {
  if (Array.isArray(node)) {
    for (const item of node) walk(item, found);
    return;
  }
  if (node === null || typeof node !== "object") return;
  for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
    if (key === "count") {
      const count = asCount(value);
      if (count !== null) found.push(count);
    }
    walk(value, found);
  }
}

function asCount(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && /^-?\d+$/.test(value)) return Number(value);
  return null;
}

function containsValue(node: unknown, value: string): boolean {
  if (node === value) return true;
  if (typeof node === "string") return false;
  if (Array.isArray(node)) return node.some((item) => containsValue(item, value));
  if (node !== null && typeof node === "object") {
    return Object.values(node as Record<string, unknown>).some((item) => containsValue(item, value));
  }
  return false;
}

function isIp(value: string): boolean {
  if (/^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$/.test(value)) return true;
  return value.includes(":") && /^[0-9a-fA-F:]+$/.test(value) && value.split(":").length > 2;
}

function isTimestamp(value: string): boolean {
  if (/^\d{4}-\d{2}-\d{2}(?:[T ][\d:.Z+-]+)?$/.test(value)) return true;
  if (/^\d{2}:\d{2}:\d{2}$/.test(value)) return true;
  return /^\d{10,13}$/.test(value);
}
