// Contract 1 of 5 (issue #589). Consumed by WS-A (registry, dispatch), WS-C
// (#595 query port, #600 Elastic, #602 Splunk) and WS-D (#596 hunt tools).

export interface ToolBounds {
  readonly maxRows: number;
  readonly timeoutMs: number;
}

// timeout and unavailable are genuine visibility gaps; refused and invalid_args
// are defects and must never be recorded as one (CONTEXT.md, Visibility gap).
export type ToolFailure =
  | { kind: "invalid_args"; detail: string }
  | { kind: "refused"; detail: string }
  | { kind: "timeout"; timeoutMs: number }
  | { kind: "unavailable"; detail: string }
  | { kind: "backend_error"; detail: string };

export type ToolResult =
  | { ok: true; rows: readonly unknown[]; rowCount: number; capped: boolean; sourceSystem: string }
  | { ok: false; failure: ToolFailure };

// What an adapter author writes. Receives the bounds and must enforce them at
// the source: capping a serialised result afterwards is what ADR-0004 forbids.
export interface ToolAdapter {
  readonly id: string;
  readonly description: string;
  readonly parameters: Record<string, unknown>;
  execute(args: Record<string, unknown>, bounds: ToolBounds, signal: AbortSignal): Promise<ToolResult>;
  close?(): Promise<void>;
}

declare const registered: unique symbol;

// The only tool shape the registry and the harness ever hold. Unconstructable
// outside defineTool, which is how a caller is prevented from opting out of bounds.
export interface RegisteredTool {
  readonly [registered]: true;
  readonly id: string;
  readonly description: string;
  readonly parameters: Record<string, unknown>;
  readonly bounds: ToolBounds;
  invoke(args: Record<string, unknown>): Promise<ToolResult>;
  close(): Promise<void>;
}

// An adapter returning more rows than it was told to is a defect in the adapter,
// so it throws rather than resolving: truncating here is what ADR-0004 forbids.
export class ToolBoundsViolation extends Error {}

// The brand is type-only and is never materialised, so the cast is the whole
// mechanism: an object literal cannot satisfy RegisteredTool on its own.
export function defineTool(adapter: ToolAdapter, bounds: ToolBounds): RegisteredTool {
  return {
    id: adapter.id,
    description: adapter.description,
    parameters: adapter.parameters,
    bounds,
    invoke: (args) => invokeBounded(adapter, bounds, args),
    close: async () => void (await adapter.close?.()),
  } as RegisteredTool;
}

async function invokeBounded(
  adapter: ToolAdapter,
  bounds: ToolBounds,
  args: Record<string, unknown>,
): Promise<ToolResult> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), bounds.timeoutMs);
  try {
    const result = await adapter.execute(args, bounds, controller.signal);
    if (result.ok && result.rowCount > bounds.maxRows) {
      throw new ToolBoundsViolation(
        `${adapter.id} returned ${result.rowCount} rows against a cap of ${bounds.maxRows}`,
      );
    }
    return result;
  } catch (error) {
    if (error instanceof ToolBoundsViolation) throw error;
    if (controller.signal.aborted) return { ok: false, failure: { kind: "timeout", timeoutMs: bounds.timeoutMs } };
    return { ok: false, failure: { kind: "backend_error", detail: messageOf(error) } };
  } finally {
    clearTimeout(timer);
  }
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
