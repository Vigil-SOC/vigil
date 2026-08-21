import pLimit from "p-limit";

export interface RateLimit {
  rpm: number;
  tpm: number;
}

// The gateway saying its own budget is gone. Distinct from a Refusal, which is
// this layer's pool declining a call it has not yet made.
export class GatewayExhausted extends Error {}

const RETRYABLE = new Set([429, 500, 502, 503]);

// Statuses that mean a ceiling was reached rather than a server that stumbled. They
// answer the same way every attempt, so they get one extra try rather than three.
const CEILING = new Set([408, 504]);

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function statusOf(error: unknown): number | undefined {
  const status = (error as { status?: unknown }).status;
  return typeof status === "number" ? status : undefined;
}

function retryAfterMs(error: unknown): number | undefined {
  const headers = (error as { headers?: Record<string, string> }).headers;
  const raw = headers?.["retry-after"];
  if (raw === undefined) return undefined;
  const seconds = Number(raw);
  return Number.isFinite(seconds) ? seconds * 1000 : undefined;
}

// Continuous-refill token bucket. A request larger than the whole bucket is
// clamped rather than allowed to wait forever.
class Bucket {
  private available: number;
  private last = Date.now();

  constructor(
    private readonly capacity: number,
    private readonly perMs: number,
  ) {
    this.available = capacity;
  }

  private refill(): void {
    const now = Date.now();
    this.available = Math.min(this.capacity, this.available + (now - this.last) * this.perMs);
    this.last = now;
  }

  async take(amount: number): Promise<void> {
    const wanted = Math.min(amount, this.capacity);
    for (;;) {
      this.refill();
      if (this.available >= wanted) {
        this.available -= wanted;
        return;
      }
      await sleep(Math.ceil((wanted - this.available) / this.perMs));
    }
  }
}

// Shared across a run's concurrent calls: RPM and TPM buckets, a concurrency gate,
// and jittered backoff so callers that hit a 429 together do not retry in step.
export class Limiter {
  private readonly requests: Bucket;
  private readonly tokens: Bucket;
  private readonly gate: ReturnType<typeof pLimit>;

  constructor(
    rate: RateLimit,
    concurrency: number,
    private readonly attempts = 3,
  ) {
    this.requests = new Bucket(rate.rpm, rate.rpm / 60_000);
    this.tokens = new Bucket(rate.tpm, rate.tpm / 60_000);
    this.gate = pLimit(concurrency);
  }

  async run<T>(estimatedTokens: number, call: () => Promise<T>): Promise<T> {
    return this.gate(async () => {
      await Promise.all([this.requests.take(1), this.tokens.take(estimatedTokens)]);
      return this.withRetry(call);
    });
  }

  private async withRetry<T>(call: () => Promise<T>): Promise<T> {
    let lastError: unknown;
    let blind = 0;
    for (let attempt = 0; attempt < this.attempts; attempt += 1) {
      try {
        return await call();
      } catch (error) {
        const status = statusOf(error);
        if (status === 402) throw new GatewayExhausted((error as Error).message);
        // One more attempt, not three: retrying a ceiling costs a role its whole dispatch
        // in wall clock. A dead socket is the same ceiling with nobody left to answer.
        if (status === undefined || CEILING.has(status)) {
          blind += 1;
          if (blind > 1) throw error;
        } else if (!RETRYABLE.has(status)) {
          throw error;
        }
        lastError = error;
        if (attempt === this.attempts - 1) break;
        const backoff = retryAfterMs(error) ?? 2 ** attempt * 500;
        await sleep(backoff + Math.random() * 250);
      }
    }
    throw lastError;
  }
}

// 4 chars per token is the standard rough estimate; it only has to be close
// enough to keep the TPM bucket honest before the real usage comes back.
export function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}
