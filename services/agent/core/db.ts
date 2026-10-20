import type { PoolConfig } from "pg";

// DATABASE_URL wins so CI and the integration test keep passing a DSN, but a
// deployed process has no DSN to pass: the chart ships discrete POSTGRES_* vars
// because Kubernetes `$(VAR)` substitution does no URL-encoding, so a password
// containing @ or / would render a malformed one (infra/helm/vigil/templates/
// _env.tpl). pg takes the parts, so nothing needs encoding either way.
//
// Here rather than in worker.ts because both entry points open a pool: the worker
// drains the queue and serve answers the HTTP surface, and they are separate
// Deployments since #635.
export function poolConfig(): PoolConfig {
  const url = process.env["DATABASE_URL"];
  if (url !== undefined && url !== "") return { connectionString: url };

  const host = process.env["POSTGRES_HOST"];
  if (host === undefined || host === "") {
    throw new Error("set DATABASE_URL, or POSTGRES_HOST and the other POSTGRES_* vars");
  }
  return {
    host,
    port: Number(process.env["POSTGRES_PORT"] ?? 5432),
    database: process.env["POSTGRES_DB"],
    user: process.env["POSTGRES_USER"],
    password: process.env["POSTGRES_PASSWORD"],
  };
}

export interface RedisConfig {
  host: string;
  port: number;
  db: number;
  password?: string;
  // TLS, which ioredis reads from this same field. Set from the URL's scheme
  // below: an external Redis reaches the agent as a URL and nowhere else.
  tls?: Record<string, never>;
}

// The parts win, unlike poolConfig above: vigil.env hands every pod a REDIS_URL
// for Python's sake, and under Bitnami auth the kubelet substitutes the password
// into it unencoded, so one holding @ / : or # parses to the wrong thing.
export function redisConfig(): RedisConfig {
  const host = process.env["REDIS_HOST"];
  if (host !== undefined && host !== "") {
    const password = process.env["REDIS_PASSWORD"];
    return {
      host,
      port: Number(process.env["REDIS_PORT"] ?? 6379),
      db: Number(process.env["REDIS_DB"] ?? 0),
      ...(password === undefined || password === "" ? {} : { password }),
    };
  }

  // Decoded: new URL() leaves the password percent-encoded.
  const url = new URL(process.env["REDIS_URL"] ?? "redis://localhost:6379/0");
  return {
    host: url.hostname,
    port: Number(url.port || 6379),
    db: Number(url.pathname.slice(1) || 0),
    ...(url.password === "" ? {} : { password: decodeURIComponent(url.password) }),
    // A rediss:// URL asks for TLS, and reading only host/port/password off it
    // would dial the TLS port in the clear. Azure Cache for Redis answers a
    // plaintext client with nothing at all -- no error to log, just a socket
    // that never speaks -- so the connection churns through retries, BullMQ
    // never takes the queue, and /readyz answers 503 while /healthz stays green.
    // The scheme is the only place this is said: the discrete REDIS_HOST form is
    // emitted for an in-cluster Redis alone, which has no TLS to ask for.
    ...(url.protocol === "rediss:" ? { tls: {} } : {}),
  };
}
