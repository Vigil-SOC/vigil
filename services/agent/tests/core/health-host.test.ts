import { afterEach, describe, expect, it, vi } from "vitest";
import { agentBindHost } from "../../core/health.js";

afterEach(() => vi.unstubAllEnvs());

describe("agentBindHost", () => {
  it("is undefined when unset, so listeners keep all interfaces", () => {
    vi.stubEnv("AGENT_BIND_HOST", "");
    expect(agentBindHost()).toBeUndefined();
  });

  it("returns the configured host", () => {
    vi.stubEnv("AGENT_BIND_HOST", "127.0.0.1");
    expect(agentBindHost()).toBe("127.0.0.1");
  });
});
