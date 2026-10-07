import { describe, expect, it } from "vitest";
import { httpVirtualKey } from "../../core/vk.js";

function reader(answers: (() => Response)[], seen: string[] = []) {
  let clock = 0;
  const fetch = (async (url: string) => {
    seen.push(String(url));
    const next = answers.shift();
    if (next === undefined) throw new Error("no answer left");
    return next();
  }) as unknown as typeof globalThis.fetch;
  const vk = httpVirtualKey({ url: "http://backend/internal/pricing/", token: "t", ttlMs: 1000, fetch, now: () => clock, warn: () => {} });
  return { vk, tick: (ms: number) => (clock += ms) };
}

const answer = (body: unknown, status = 200) => () => ({ ok: status < 400, status, json: async () => body }) as Response;

describe("the virtual key reader", () => {
  it("asks the backend once per interval, and trusts a null answer as long as a key", async () => {
    const seen: string[] = [];
    const { vk, tick } = reader([answer({ vk: null }), answer({ vk: "sk-bf-new" })], seen);
    expect(await vk()).toBeNull();
    expect(await vk()).toBeNull();
    expect(seen).toEqual(["http://backend/internal/pricing/vk"]);
    tick(1000);
    expect(await vk()).toBe("sk-bf-new");
  });

  // A blip must neither block traffic nor pin "no key" for a whole interval.
  it("sends no key when the lookup fails, and asks again on the next call", async () => {
    const { vk } = reader([answer({ detail: "boom" }, 500), () => { throw new Error("down"); }, answer({ vk: "sk-bf-x" })]);
    expect(await vk()).toBeNull();
    expect(await vk()).toBeNull();
    expect(await vk()).toBe("sk-bf-x");
  });

  it("gives up on a backend that never answers rather than holding the model call", async () => {
    const hung = ((_url: string, init: RequestInit) =>
      new Promise((_resolve, reject) => init.signal?.addEventListener("abort", () => reject(init.signal?.reason)))) as unknown as typeof globalThis.fetch;
    const vk = httpVirtualKey({ url: "http://backend/internal/pricing", token: "t", ttlMs: 1000, timeoutMs: 20, fetch: hung, warn: () => {} });
    expect(await vk()).toBeNull();
  });
});
