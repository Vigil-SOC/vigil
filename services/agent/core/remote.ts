import { SpanStatusCode } from "@opentelemetry/api";
import type { RegisteredTool, ToolPrincipal, ToolResult } from "../contracts/tool.js";
import type { SkillBodies } from "./loop.js";
import type { CallApproval, ToolDispatch } from "./seams.js";
import { capture, traceHeaders, traced } from "./tracing.js";
import { SKILL_TOOL } from "./record.js";
import { errorFields, logger } from "./log.js";

const log = logger("agent.remote");

export interface RemoteOptions {
  url: string;
  token: string;
  // Sent with every call when present; left off the body entirely when not.
  principal?: ToolPrincipal;
  fetch?: typeof globalThis.fetch;
}

const FAILURE_KINDS = new Set(["invalid_args", "refused", "denied", "timeout", "unavailable", "backend_error"]);

function unavailable(detail: string): ToolResult {
  return { ok: false, failure: { kind: "unavailable", detail } };
}

// The far side owns the discrimination, so anything that does not arrive as a
// ToolResult is this hop failing rather than the tool, and reads as unavailable.
function resultOf(body: unknown): ToolResult {
  if (typeof body !== "object" || body === null) return unavailable("the endpoint did not answer with a result");
  const value = body as Record<string, unknown>;
  if (value["ok"] === true && Array.isArray(value["rows"])) return value as unknown as ToolResult;
  const failure = value["failure"];
  if (typeof failure === "object" && failure !== null && FAILURE_KINDS.has(String((failure as Record<string, unknown>)["kind"]))) {
    return { ok: false, failure } as ToolResult;
  }
  return unavailable("the endpoint answered with neither rows nor a known failure");
}

// One signal for a call that must end on whichever comes first: its own deadline
// or the run letting go. Shared with the keyed memory read, which needs the same
// two and would otherwise write them again.
//
// Wired by hand rather than with AbortSignal.any, which attaches to the run's
// signal on every call and lets go only when the composite is collected: one long
// run piles a listener per call onto the one signal until Node warns about it.
// This one unwires in release.
export interface Deadline {
  signal: AbortSignal;
  timedOut: () => boolean;
  release: () => void;
}

export function deadline(timeoutMs: number, signal?: AbortSignal): Deadline {
  const timeout = AbortSignal.timeout(timeoutMs);
  const halt = new AbortController();
  const relay = (source: AbortSignal) => () => halt.abort(source.reason);
  const onTimeout = relay(timeout);
  const onAbort = signal === undefined ? undefined : relay(signal);
  if (signal?.aborted === true) halt.abort(signal.reason);
  timeout.addEventListener("abort", onTimeout, { once: true });
  if (onAbort !== undefined) signal?.addEventListener("abort", onAbort, { once: true });

  return {
    signal: halt.signal,
    timedOut: () => timeout.aborted,
    release: () => {
      timeout.removeEventListener("abort", onTimeout);
      if (onAbort !== undefined) signal?.removeEventListener("abort", onAbort);
    },
  };
}

// An operator skill's current text, read from the backend beside skill-reads. A
// refusal there is an error status with a detail; it comes back as the failure the
// model reads, never a throw.
export function skillBodies(options: RemoteOptions): SkillBodies {
  const call = options.fetch ?? globalThis.fetch;
  const bodies = new URL("skill-bodies", options.url).toString();
  return async (name) => {
    let response: Response;
    try {
      response = await call(bodies, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${options.token}`, ...traceHeaders() },
        body: JSON.stringify({ name }),
        signal: AbortSignal.timeout(8_000),
      });
    } catch (error) {
      return { ok: false, kind: "unavailable", detail: `reading ${name} failed: ${error instanceof Error ? error.message : String(error)}` };
    }
    if (response.ok) {
      try {
        const answer = (await response.json()) as { body?: unknown };
        if (typeof answer.body === "string") return { ok: true, body: answer.body };
      } catch {
        // Falls through to the unreadable answer below.
      }
      return { ok: false, kind: "unavailable", detail: `the answer for ${name} could not be read` };
    }
    const detail = await response
      .json()
      .then((answer: unknown) => {
        const detail = (answer as { detail?: unknown } | null)?.detail;
        return typeof detail === "string" ? detail : "";
      })
      .catch(() => "");
    const kind = response.status === 404 ? "invalid_args" : response.status === 403 || response.status === 409 || response.status === 423 ? "refused" : "unavailable";
    return { ok: false, kind, detail: detail === "" ? `the endpoint answered ${response.status} for ${name}` : detail };
  };
}

// Satisfies the same port as localDispatch, so neither the loop nor a workflow
// knows whether a tool runs in this process. Bounds travel and are applied there.
export function remoteDispatch(options: RemoteOptions): ToolDispatch {
  const call = options.fetch ?? globalThis.fetch;
  const send = async (
    tool: RegisteredTool,
    args: Record<string, unknown>,
    signal?: AbortSignal,
    agentId?: string,
    approval?: CallApproval,
  ): Promise<ToolResult> => {
    // A tool the registry answers itself never leaves the process: every call routes
    // through this dispatch, so a local implementation was otherwise unreachable.
    if (tool.local) {
      const result = await tool.invoke(args);
      if (tool.id === SKILL_TOOL && result.ok) noteSkillRead(args["name"], agentId);
      return result;
    }

    // The tool's timeout and the run's abort both end this call, so whichever
    // fires first does: a lost lease must not wait out a 30s tool.
    const held = deadline(tool.bounds.timeoutMs, signal);

    // Unwired around the whole call, body included: reading the body is still
    // work the run's abort should be able to stop.
    try {
      let response: Response;
      try {
        response = await call(options.url, {
          method: "POST",
          // The tool span's context, so the backend's spans for this call nest under it.
          headers: { "content-type": "application/json", authorization: `Bearer ${options.token}`, ...traceHeaders() },
          body: JSON.stringify({
            tool: tool.id,
            args,
            bounds: { max_rows: tool.bounds.maxRows, timeout_ms: tool.bounds.timeoutMs },
            ...(options.principal === undefined ? {} : { principal: options.principal }),
            // The agent rides the call, not the dispatch options: one
            // dispatch serves every phase of a compose run (#1560).
            ...(agentId === undefined ? {} : { agent_id: agentId }),
            // Present only for a call the run parked and a person released.
            ...(approval === undefined ? {} : { run_id: approval.runId, checkpoint_id: approval.checkpointId }),
          }),
          signal: held.signal,
        });
      } catch (error) {
        // The far side never answered, so nothing is known about the tool itself.
        // The run first, and before the deadline: when both have fired, nobody is
        // waiting for this answer, and an aborted run is not a tool that failed.
        if (signal?.aborted === true) return unavailable("the run was aborted");
        // Read off the timer rather than off the error's name: a run whose own
        // signal is a timeout aborts with a TimeoutError, which is the run's
        // deadline and not this tool's.
        if (held.timedOut()) return { ok: false, failure: { kind: "timeout", timeoutMs: tool.bounds.timeoutMs } };
        return unavailable(error instanceof Error ? error.message : String(error));
      }

      if (!response.ok) return unavailable(`the endpoint answered ${response.status}`);
      try {
        return resultOf(await response.json());
      } catch (error) {
        return unavailable(error instanceof Error ? error.message : String(error));
      }
    } finally {
      held.release();
    }
  };

  // A skill a run loaded counts as a use of it, like a read_skill call the backend
  // answers (core/skills/skill_usage.py). The backend owns that count, so it is told;
  // not awaited, since a usage count must never slow or fail the call it counts.
  const reads = new URL("skill-reads", options.url).toString();
  const noteSkillRead = (name: unknown, agentId?: string): void => {
    if (typeof name !== "string" || name === "") return;
    call(reads, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${options.token}` },
      body: JSON.stringify({ name, ...(agentId === undefined ? {} : { agent_id: agentId }) }),
      signal: AbortSignal.timeout(5_000),
    })
      .then((response) => {
        if (!response.ok) log.warn("recording a skill read was refused", { skill: name, status: response.status });
      })
      .catch((error: unknown) => log.warn("recording a skill read failed", { skill: name, ...errorFields(error) }));
  };

  return {
    // Opened around local tools too: send() answers them inside this span.
    invoke: (tool, args, signal, agentId, approval, callId) => {
      const attributes = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": tool.id,
        ...(callId === undefined ? {} : { "gen_ai.tool.call.id": callId }),
        ...(agentId === undefined ? {} : { "gen_ai.agent.id": agentId }),
      };
      return traced(`execute_tool ${tool.id}`, attributes, async (span) => {
        capture(span, "gen_ai.tool.call.arguments", args);
        const result = await send(tool, args, signal, agentId, approval);
        // A failed tool is a result the model reads, not a throw, so it is marked here.
        if (!result.ok) span.setStatus({ code: SpanStatusCode.ERROR, message: result.failure.kind });
        capture(span, "gen_ai.tool.call.result", result);
        return result;
      });
    },
  };
}
