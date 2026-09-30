import { quote, type Siem } from "./siem.js";

// One sourcetype as the store reports it: how much it holds, over what span, and
// the field names its events actually carry.
export interface Source {
  index: string;
  sourcetype: string;
  count: number;
  first: number;
  last: number;
  fields: string[];
}

export interface Survey {
  sources: Source[];
  // Set when the store could not be surveyed at all, which the run reports rather
  // than tracing over a blank.
  error?: string;
}

// Enough to cover what a trace crosses without one survey costing a run's worth of
// searches; a sourcetype past these is still searchable, its fields read on demand.
const MAX_SOURCES = 30;
const FIELDS_SHOWN = 60;

// Run by code before the first turn, so every source is in front of the model
// before one of them takes all its attention, with the field names it would
// otherwise guess -- and a guessed name returns zero rows, not an error.
export async function survey(siem: Siem, signal?: AbortSignal): Promise<Survey> {
  const listed = await siem.search(
    `| tstats count min(_time) as first max(_time) as last where index=* by index sourcetype | sort - count | head ${MAX_SOURCES}`,
    {},
    MAX_SOURCES,
    signal,
  );
  if (listed.error !== undefined) return { sources: [], error: listed.error };
  const sources = await Promise.all(
    listed.rows.map(async (row): Promise<Source> => {
      const index = String(row["index"] ?? "");
      const sourcetype = String(row["sourcetype"] ?? "");
      return {
        index,
        sourcetype,
        count: Number(row["count"] ?? 0),
        first: Number(row["first"] ?? 0),
        last: Number(row["last"] ?? 0),
        fields: await fieldsOf(siem, index, sourcetype, signal),
      };
    }),
  );
  return { sources: sources.filter((source) => source.sourcetype !== "") };
}

// Real field names of one sourcetype, sampled. Empty when the sample could not be
// read: no names is "unknown", which the field check treats as nothing to say.
export async function fieldsOf(siem: Siem, index: string, sourcetype: string, signal?: AbortSignal): Promise<string[]> {
  const scope = index === "" ? "index=*" : `index=${quote(index)}`;
  const sampled = await siem.search(
    `${scope} sourcetype=${quote(sourcetype)} | head 2000 | fieldsummary | where count > 0 | fields field`,
    {},
    500,
    signal,
  );
  if (sampled.error !== undefined) return [];
  return sampled.rows
    .map((row) => String(row["field"] ?? ""))
    .filter((field) => field !== "" && !field.startsWith("_") && !/^date_|^punct$|^linecount$|^splunk_server|^timestartpos|^timeendpos/.test(field));
}

// What a lookup the run makes for itself searches over: every index the survey
// found, never the store's internal ones.
export function scopeOf(found: Survey): string {
  const indexes = [...new Set(found.sources.map((source) => source.index).filter(Boolean))];
  if (indexes.length === 0) return "index=*";
  return indexes.length === 1 ? `index=${quote(indexes[0]!)}` : `(${indexes.map((index) => `index=${quote(index)}`).join(" OR ")})`;
}

// The first event time of each sourcetype, epoch seconds.
export function startsOf(found: Survey): Map<string, number> {
  const starts = new Map<string, number>();
  for (const source of found.sources) {
    const held = starts.get(source.sourcetype);
    if (held === undefined || source.first < held) starts.set(source.sourcetype, source.first);
  }
  return starts;
}

const minute = (epoch: number): string =>
  Number.isFinite(epoch) && epoch > 0 ? new Date(epoch * 1000).toISOString().slice(0, 16).replace("T", " ") : "?";

export function renderSurvey(found: Survey): string {
  if (found.error !== undefined) return `## Your environment\n\nThe log store could not be surveyed: ${found.error}`;
  const lines = found.sources.map((source) => {
    const shown = source.fields.slice(0, FIELDS_SHOWN).join(", ");
    const more = source.fields.length > FIELDS_SHOWN ? ` …+${source.fields.length - FIELDS_SHOWN}` : "";
    return (
      `- index=${source.index} sourcetype=${source.sourcetype}: ${source.count.toLocaleString("en-US")} events, ` +
      `${minute(source.first)} → ${minute(source.last)} UTC\n  fields: ${shown}${more}`
    );
  });
  return [
    "## Your environment (surveyed by code before your first turn)",
    `${found.sources.length} sourcetype(s), largest first.`,
    ...lines,
  ].join("\n\n");
}
