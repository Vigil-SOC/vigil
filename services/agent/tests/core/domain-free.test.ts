import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const CORE = join(import.meta.dirname, "..", "..", "core");

// The domain-free requirement, enforced rather than reviewed. A grep is enough:
// reaching for the nearest word of a domain leaves the word behind.
const FORBIDDEN: readonly RegExp[] = [
  /\bhypothes\w*/i,
  /\bverdicts?\b/i,
  /\bproven\b/i,
  /\bhunts?\b/i,
  /\bhunting\b/i,
  /\bevidence\b/i,
  /\btelemetry\b/i,
  /\bthreats?\b/i,
  /\btriage\b/i,
  /\bincidents?\b/i,
  /\balerts?\b/i,
  /\bsiem\b/i,
  /\bcontainment\b/i,
  /\bmalware\b/i,
  /\badversar\w*/i,
  /\bintrusion\b/i,
];

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sources(path);
    return entry.name.endsWith(".ts") ? [path] : [];
  });
}

const FILES = sources(CORE);

// The kernel's record shares its tool names (propose_hypothesis, link_evidence) with the
// hunt, so this file alone may use those two words; every other word still holds.
const RECORD = "record.ts";
const RECORD_WORDS: ReadonlySet<string> = new Set([String.raw`\bhypothes\w*`, String.raw`\bevidence\b`]);

describe("the harness is domain-free", () => {
  // A vacuous pass is the one way this check fails silently.
  it("has files to check", () => {
    expect(FILES.length).toBeGreaterThan(8);
  });

  it.each(FILES.map((file) => [file.slice(CORE.length + 1), file]))("%s names no domain", (_name, file) => {
    const source = readFileSync(file, "utf8");
    const allowed = file.endsWith(`/${RECORD}`) ? RECORD_WORDS : new Set<string>();
    const found = FORBIDDEN.filter((pattern) => pattern.test(source) && !allowed.has(pattern.source)).map((pattern) => pattern.source);
    expect(found).toEqual([]);
  });

  // The harness may hold a workflow's events and call its tools; it may not know
  // which workflow. Importing one is how that distinction gets lost.
  it("imports no workflow", () => {
    const offenders = FILES.filter((file) => /from\s+"[^"]*workflows\//.test(readFileSync(file, "utf8")));
    expect(offenders).toEqual([]);
  });
});
