// The Bifrost virtual key a model call carries as x-bf-vk, or null for none. Asked of
// the backend, which alone decides whether the budget is enforced; null is an answer
// (bypass, or no key configured), not a failure.
export type VirtualKey = () => Promise<string | null>;

export const noVirtualKey: VirtualKey = async () => null;

export interface VirtualKeyOptions {
  url: string;
  token: string;
  // The backend's refresh interval, as for prices: a key changed in Settings
  // reaches agent traffic within one of these.
  ttlMs: number;
  // The lookup sits inside the model call's limiter slot, so a hung backend must
  // cost a few seconds and no header, not the fetch default of minutes.
  timeoutMs?: number;
  fetch?: typeof globalThis.fetch;
  now?: () => number;
  warn?: (message: string) => void;
}

// Memoised for ttlMs, null included. A failed lookup is not memoised and sends no
// header, as get_active_vk() does on a DB error: a broken lookup must never block
// model traffic. Never throws.
export function httpVirtualKey(options: VirtualKeyOptions): VirtualKey {
  const call = options.fetch ?? globalThis.fetch;
  const now = options.now ?? Date.now;
  const warn = options.warn ?? console.warn;
  const url = `${options.url.replace(/\/$/, "")}/vk`;
  let held: { vk: string | null; at: number } | undefined;

  return async () => {
    if (held !== undefined && now() - held.at < options.ttlMs) return held.vk;
    try {
      const response = await call(url, {
        headers: { "content-type": "application/json", authorization: `Bearer ${options.token}` },
        signal: AbortSignal.timeout(options.timeoutMs ?? 5_000),
      });
      if (!response.ok) throw new Error(`answered ${response.status}`);
      const body = (await response.json()) as { vk?: unknown } | null;
      const raw = body?.vk;
      if (raw !== null && typeof raw !== "string") throw new Error("answered without a vk field");
      const vk = raw === null || raw.trim() === "" ? null : raw.trim();
      held = { vk, at: now() };
      return vk;
    } catch (error) {
      warn(`virtual key lookup failed, sending no x-bf-vk: ${error instanceof Error ? error.message : String(error)}`);
      return null;
    }
  };
}
