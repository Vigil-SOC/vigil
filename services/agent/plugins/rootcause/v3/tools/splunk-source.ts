import { domainsIn, sizesIn, type Survey } from "../survey.js";
import type { Job, JobOpts, JobRunner, Row } from "./job.js";
import { LiveSearch } from "./search.js";
import type { Invoke, Source, SourceAdapter, SourceScope } from "./source.js";
import { splunkTool } from "./splunk-tool.js";

// Splunk through Vigil's own server, splunk-selfhosted's splunk_execute. It answers one
// envelope, delivered as a single row: the rows under results, the events the search
// matched as event_count, Splunk's warnings, and on failure Splunk's own reason as the
// call's backend_error.

export const SPLUNK_EXECUTE = "splunk-selfhosted_splunk_execute";

const FIELDS = 60; // per sourcetype, in the survey

interface Envelope {
  results?: unknown;
  count?: unknown;
  event_count?: unknown;
  warnings?: unknown;
}

const number = (value: unknown, fallback: number) => (typeof value === "number" && Number.isFinite(value) ? value : fallback);

export function splunkRunner(invoke: Invoke): JobRunner {
  return {
    async runJob(spl: string, o: JobOpts): Promise<Job> {
      const started = Date.now();
      const answer = await invoke({ spl_query: spl, earliest: String(Math.floor(o.earliest)), latest: String(Math.ceil(o.latest)), max_results: o.limit ?? 100 });
      const seconds = (Date.now() - started) / 1000;
      if (!answer.ok) {
        const f = answer.failure;
        // A search Splunk did not finish in time comes back with what is known: nothing, partial.
        if (f.kind === "timeout") return { rows: [], events: 0, results: 0, finalized: true, errors: [], warnings: [], seconds };
        return { rows: [], events: 0, results: 0, finalized: false, errors: [f.detail], warnings: [], seconds };
      }
      const envelope = (answer.rows[0] ?? {}) as Envelope;
      const rows = (Array.isArray(envelope.results) ? envelope.results : []) as Row[];
      const warnings = Array.isArray(envelope.warnings) ? envelope.warnings.map(String) : [];
      return {
        rows,
        // A server without event_count (before it reported one) counts what it returned.
        events: number(envelope.event_count, rows.length),
        results: number(envelope.count, rows.length),
        finalized: false,
        errors: [],
        warnings,
        seconds,
      };
    },
  };
}

const day = (v: unknown) => new Date(Number(v) * 1000).toISOString().slice(0, 16).replace("T", " ");

// Every sourcetype in the index with its event count, span and real field names. Built
// from the index, so it works on data nobody described; one domain per sourcetype.
export async function surveySplunk(runner: JobRunner, live: LiveSearch, scope: SourceScope): Promise<Survey> {
  const range = { earliest: Date.parse(scope.range[0]) / 1000, latest: Date.parse(scope.range[1]) / 1000, limit: 1000 };
  const srcs = await runner.runJob(`| tstats count min(_time) as first max(_time) as last where index=${scope.index} by sourcetype | sort - count`, range);
  if (srcs.errors.length) throw new Error(`the survey of index ${scope.index} failed: ${srcs.errors.join("; ")}`);
  const parts = await Promise.all(
    srcs.rows.map(async (r) => {
      const fields = await live.fieldsIn(String(r["sourcetype"]));
      return `- ${String(r["sourcetype"])}: ${Number(r["count"]).toLocaleString("en-US")} events, ${day(r["first"])} → ${day(r["last"])}\n  fields: ${fields.slice(0, FIELDS).join(", ")}${fields.length > FIELDS ? ` …+${fields.length - FIELDS}` : ""}`;
    }),
  );
  const text = `## Your environment (surveyed by code before your first turn)\n\nSplunk index ${scope.index}: ${srcs.rows.length} sourcetypes.\n\n${parts.join("\n")}`;
  return { text, domains: domainsIn(text), sizes: sizesIn(text) };
}

export const splunkAdapter: SourceAdapter = {
  backend: "splunk-selfhosted",
  tools: [SPLUNK_EXECUTE],
  open(invoke: Invoke, scope: SourceScope): Source {
    const runner = splunkRunner(invoke);
    const live = new LiveSearch(runner, scope.index, scope.range);
    return {
      backend: "splunk-selfhosted",
      language: "spl",
      evidenceTool: () => splunkTool(live, scope.index),
      survey: () => surveySplunk(runner, live, scope),
    };
  },
};
