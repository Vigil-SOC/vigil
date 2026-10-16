// Recorded results and scripted turns, read from the fixtures, so a whole run replays
// without Splunk or a model.
import { readFileSync } from "node:fs";
import type { QueryInput, SearchBackend } from "../../../plugins/rootcause/v3/tools/search.js";
import type { ScriptEntry } from "../../../plugins/rootcause/v3/model/client.js";

// Replays results by exact input; a search not on the tape says so, as a failed search does.
export class Cassette implements SearchBackend {
  private tape = new Map<string, string>();
  constructor(private path: string) {
    for (const line of readFileSync(path, "utf8").split("\n").filter(Boolean)) {
      const { key, result } = JSON.parse(line) as { key: string; result: string };
      this.tape.set(key, result);
    }
  }
  static key(i: QueryInput, op?: "count") {
    return JSON.stringify({ ...(op ? { op } : {}), spl: i.spl.trim(), earliest: i.earliest ?? null, latest: i.latest ?? null });
  }
  async count(input: QueryInput): Promise<number> {
    const hit = this.tape.get(Cassette.key(input, "count"));
    return hit === undefined ? -1 : Number(hit);
  }
  async search(input: QueryInput): Promise<string> {
    const key = Cassette.key(input);
    return this.tape.get(key) ?? `ERROR: no recorded result for this search (cassette ${this.path}): ${key}`;
  }
}

export function scriptFrom(path: string): ScriptEntry[] {
  const text = readFileSync(path, "utf8").trim();
  return text.startsWith("[") ? JSON.parse(text) : text.split("\n").filter(Boolean).map((l) => JSON.parse(l));
}
