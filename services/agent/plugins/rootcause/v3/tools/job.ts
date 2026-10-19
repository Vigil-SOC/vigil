// What one search job gives back: its rows, its true counts, and Splunk's own errors and
// warnings, which a model needs verbatim to fix a search. Whatever runs the job (a Splunk
// connection, a recorded cassette) answers in this shape.
export type Row = Record<string, unknown>;

export interface Job {
  rows: Row[];
  events: number;
  results: number;
  finalized: boolean; // hit the time limit; counts are partial
  errors: string[];
  warnings: string[];
  seconds: number;
}

export interface JobOpts {
  earliest: number; // epoch seconds
  latest: number;
  limit?: number;
  timeoutMs?: number;
}

export interface JobRunner {
  runJob(spl: string, o: JobOpts): Promise<Job>;
}
