import type { RegisteredTool, ToolResult } from "../contracts/tool.js";
import type { ToolDispatch } from "./seams.js";

export interface RemoteOptions {
  url: string;
  token: string;
  fetch?: typeof globalThis.fetch;
}

const FAILURE_KINDS = new Set(["invalid_args", "refused", "timeout", "unavailable", "backend_error"]);

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

// Satisfies the same port as localDispatch, so neither the loop nor a workflow
// knows whether a tool runs in this process. Bounds travel and are applied there.
export function remoteDispatch(options: RemoteOptions): ToolDispatch {
  const call = options.fetch ?? globalThis.fetch;
  return {
    invoke: async (tool: RegisteredTool, args: Record<string, unknown>, signal?: AbortSignal): Promise<ToolResult> => {
      // A tool the registry answers itself never leaves the process: every call routes
      // through this dispatch, so a local implementation was otherwise unreachable.
      if (tool.local) return tool.invoke(args);

      // The tool's timeout and the run's abort both end this call, so whichever
      // fires first does: a lost lease must not wait out a 30s tool.
      //
      // Wired by hand rather than with AbortSignal.any, which attaches to the
      // run's signal on every call and lets go only when the composite is
      // collected: one long run piles a listener per tool call onto the one
      // signal until Node warns about it. This one unwires in the finally.
      const timeout = AbortSignal.timeout(tool.bounds.timeoutMs);
      const halt = new AbortController();
      const relay = (source: AbortSignal) => () => halt.abort(source.reason);
      const onTimeout = relay(timeout);
      const onAbort = signal === undefined ? undefined : relay(signal);
      if (signal?.aborted === true) halt.abort(signal.reason);
      timeout.addEventListener("abort", onTimeout, { once: true });
      if (onAbort !== undefined) signal?.addEventListener("abort", onAbort, { once: true });

      // Unwired around the whole call, body included: reading the body is still
      // work the run's abort should be able to stop.
      try {
        let response: Response;
        try {
          response = await call(options.url, {
            method: "POST",
            headers: { "content-type": "application/json", authorization: `Bearer ${options.token}` },
            body: JSON.stringify({
              tool: tool.id,
              args,
              bounds: { max_rows: tool.bounds.maxRows, timeout_ms: tool.bounds.timeoutMs },
            }),
            signal: halt.signal,
          });
        } catch (error) {
          // The far side never answered, so nothing is known about the tool itself.
          const timedOut = error instanceof Error && error.name === "TimeoutError";
          if (timedOut) return { ok: false, failure: { kind: "timeout", timeoutMs: tool.bounds.timeoutMs } };
          // An aborted run is not a tool that failed, so it is not recorded as one.
          if (signal?.aborted === true) return unavailable("the run was aborted");
          return unavailable(error instanceof Error ? error.message : String(error));
        }

        if (!response.ok) return unavailable(`the endpoint answered ${response.status}`);
        try {
          return resultOf(await response.json());
        } catch (error) {
          return unavailable(error instanceof Error ? error.message : String(error));
        }
      } finally {
        timeout.removeEventListener("abort", onTimeout);
        if (onAbort !== undefined) signal?.removeEventListener("abort", onAbort);
      }
    },
  };
}
