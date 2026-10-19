import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";

export const LIVE = "/healthz";
export const READY = "/readyz";

// Bounded so a probe fails rather than hangs. A handler that never answers leaks a
// socket per probe and still reads as a failure once the kubelet times out, so the
// only thing waiting buys is the leak.
const CHECK_TIMEOUT_MS = 2_000;

// Whether this process can do its job right now -- not whether it is alive.
//
// Liveness deliberately does not consult this. A worker whose Redis has dropped
// should stop being *ready*, so a rollout halts and alerting fires; restarting it
// would only reconnect to the same broken dependency, and restarting every replica
// at once against a Redis that is already struggling turns a blip into an outage.
export type Ready = () => Promise<boolean>;

async function within(check: Ready): Promise<boolean> {
  let timer: NodeJS.Timeout | undefined;
  const expired = new Promise<boolean>((answer) => {
    timer = setTimeout(() => answer(false), CHECK_TIMEOUT_MS);
  });
  try {
    return await Promise.race([check().catch(() => false), expired]);
  } finally {
    clearTimeout(timer);
  }
}

// Unauthenticated on purpose, and answered before any auth check: a kubelet carries
// no bearer token. They report up or down and nothing else, so there is nothing here
// to leak -- which is also why the body is a word rather than a diagnosis.
//
// Returns whether it took the request, so a server with routes of its own can offer
// these first and carry on if it was something else.
export async function handleHealth(req: IncomingMessage, res: ServerResponse, ready: Ready): Promise<boolean> {
  if (req.method !== "GET") return false;
  const url = req.url ?? "";

  if (url === LIVE) {
    res.writeHead(200, { "content-type": "text/plain" });
    res.end("ok");
    return true;
  }

  if (url === READY) {
    const ok = await within(ready);
    res.writeHead(ok ? 200 : 503, { "content-type": "text/plain" });
    res.end(ok ? "ready" : "not ready");
    return true;
  }

  return false;
}

// /readyz is unauthenticated and answered before the token check, so anything
// reachable can drive the check behind it. A kubelet asks every ten seconds.
const CACHE_MS = 1_000;

export function cachedReady(check: Ready, windowMs = CACHE_MS): Ready {
  let answered = -Infinity;
  let last: Promise<boolean> | null = null;
  return () => {
    const now = Date.now();
    if (last === null || now - answered >= windowMs) {
      answered = now;
      // Held, not awaited, so concurrent probes share one query.
      last = within(check);
    }
    return last;
  };
}

// The worker consumes a queue and serves nothing else, so it gets a listener for
// these two routes alone. Cheaper than an exec probe, which pays Node's startup
// every ten seconds per replica to learn the same thing.
export function healthServer(ready: Ready): Server {
  const answer = cachedReady(ready);
  return createServer((req, res) => {
    void (async () => {
      if (await handleHealth(req, res, answer)) return;
      res.writeHead(404, { "content-type": "text/plain" });
      res.end("not found");
    })();
  });
}

export function healthPort(): number {
  return Number(process.env["AGENT_HEALTH_PORT"] ?? 6990);
}

// Unset keeps all interfaces, which containers need; host-native starts set it
// to the API's loopback default.
export function bindHost(): string | undefined {
  return process.env["AGENT_BIND_HOST"] || undefined;
}
