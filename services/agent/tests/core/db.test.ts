import { afterEach, describe, expect, it } from "vitest";
import { redisConfig } from "../../core/db.js";

const KEYS = ["REDIS_URL", "REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD"];

afterEach(() => {
  for (const key of KEYS) delete process.env[key];
});

// A managed Redis (Azure Cache, ElastiCache) is reachable only over TLS, and
// reaching it in the clear does not fail loudly: the port accepts the socket and
// then says nothing, so the client retries forever and the queue never moves.
// The scheme is the only place the agent is told, which is what these hold.
describe("the Redis a deployed agent dials", () => {
  it("turns TLS on for a rediss:// URL", () => {
    process.env["REDIS_URL"] = "rediss://:s3cret@example.redis.cache.windows.net:6380/0";

    const config = redisConfig();

    expect(config.tls).toEqual({});
    expect(config.host).toBe("example.redis.cache.windows.net");
    expect(config.port).toBe(6380);
    expect(config.password).toBe("s3cret");
  });

  it("leaves a plain redis:// URL unencrypted, which is what an in-cluster Redis speaks", () => {
    process.env["REDIS_URL"] = "redis://localhost:6379/0";

    expect(redisConfig().tls).toBeUndefined();
  });

  it("decodes a password the URL left percent-encoded", () => {
    process.env["REDIS_URL"] = "rediss://:p%2Fss%3Aword%40here@example.redis.cache.windows.net:6380/0";

    expect(redisConfig().password).toBe("p/ss:word@here");
  });

  it("keeps TLS off for the discrete parts, which only ever name an in-cluster Redis", () => {
    process.env["REDIS_HOST"] = "vigil-redis-master";
    process.env["REDIS_PORT"] = "6379";

    const config = redisConfig();

    expect(config.tls).toBeUndefined();
    expect(config.host).toBe("vigil-redis-master");
  });
});
