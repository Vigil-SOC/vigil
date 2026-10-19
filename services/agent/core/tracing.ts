import { context, propagation, ROOT_CONTEXT, SpanKind, SpanStatusCode, trace, type Attributes, type Span } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-grpc";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BatchSpanProcessor, NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { errorFields, logger } from "./log.js";

const log = logger("agent.tracing");

// Fetched once and safe before init: the API's proxy tracer delegates to whatever
// provider is registered later, and stays a no-op when none ever is.
const tracer = trace.getTracer("vigil.agent");

// A tool result runs to tens of KB and a prompt further. Capped per value and per batch,
// because the collector's 4 MiB gRPC ceiling rejects a whole batch that exceeds it.
const ATTRIBUTE_LIMIT = 16_384;
const BATCH_SIZE = 32;

let provider: NodeTracerProvider | null = null;

function enabled(name: string): boolean {
  return ["true", "1", "yes"].includes((process.env[name] ?? "").toLowerCase());
}

// Python's switch, so one setting traces both sides. The SDK reads the endpoint and
// sampler itself, from the OTEL_* variables Python is given.
export function initTracing(service: string): boolean {
  if (!enabled("VIGIL_OTEL_ENABLED") || provider !== null) return provider !== null;
  try {
    provider = new NodeTracerProvider({
      resource: resourceFromAttributes({
        "service.name": service,
        "service.version": process.env["RELEASE_VERSION"] ?? "unknown",
        "deployment.environment": process.env["ENVIRONMENT"] ?? "development",
      }),
      spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter(), { maxExportBatchSize: BATCH_SIZE })],
      spanLimits: { attributeValueLengthLimit: ATTRIBUTE_LIMIT },
    });
    provider.register();
    return true;
  } catch (error) {
    // Tracing never stops a process from serving: a run untraced is still a run.
    log.warn("tracing did not start", errorFields(error));
    provider = null;
    return false;
  }
}

// Flushes what the batch still holds, so the spans of a run that ended just before
// SIGTERM are not the ones lost.
export async function shutdownTracing(): Promise<void> {
  await provider?.shutdown().catch((error: unknown) => log.warn("tracing did not flush", errorFields(error)));
  provider = null;
}

// Prompts, responses and tool I/O carry customer logs and IOCs, so they are exported
// only when asked. The ledger holds all of it anyway, under the run id on the root span.
export function capturesContent(): boolean {
  return enabled("VIGIL_OTEL_CAPTURE_CONTENT");
}

// Serialised only for a span being exported, so an unsampled call never pays for it.
export function capture(span: Span, key: string, value: unknown): void {
  if (!span.isRecording() || !capturesContent()) return;
  span.setAttribute(key, typeof value === "string" ? value : JSON.stringify(value));
}

// Headers carrying the active span to the far side of a call. Empty when tracing is
// off, since no propagator is registered then.
export function traceHeaders(): Record<string, string> {
  const carrier: Record<string, string> = {};
  propagation.inject(context.active(), carrier);
  return carrier;
}

export type Carrier = Record<string, string | string[] | undefined>;

interface TracedOptions {
  kind?: SpanKind;
  // Where the caller's trace arrived from -- request headers, a job's fields. Absent
  // continues whatever span is active in this process; present, never does.
  parent?: Carrier;
}

// The one way a span is opened here: active for fn's whole await chain, ended however
// fn leaves, and marked failed when it throws.
export function traced<T>(name: string, attributes: Attributes, fn: (span: Span) => Promise<T>, options: TracedOptions = {}): Promise<T> {
  const parent = options.parent === undefined ? context.active() : propagation.extract(ROOT_CONTEXT, options.parent);
  return tracer.startActiveSpan(name, { kind: options.kind ?? SpanKind.INTERNAL, attributes }, parent, async (span) => {
    try {
      return await fn(span);
    } catch (error) {
      failed(error, span);
      throw error;
    } finally {
      span.end();
    }
  });
}

// For a failure answered rather than thrown, which traced cannot see: the span is the
// one active where it was caught, and none is a no-op.
export function failed(error: unknown, span: Span | undefined = trace.getActiveSpan()): void {
  span?.recordException(error instanceof Error ? error : String(error));
  span?.setStatus({ code: SpanStatusCode.ERROR, message: error instanceof Error ? error.message : String(error) });
}
