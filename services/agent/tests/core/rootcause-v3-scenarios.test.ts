import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// The root-cause loop was built against evaluation scenarios whose content is their answer. This repo is
// public, so the port names none of them and quotes nothing from them: a reader of the code must not learn
// what a scenario contains.
const SCENARIOS = /\b(tbi|nightshift|szechuan)\b|investigation_ns/i;

const ROOTS = ["plugins/rootcause/v3", "tests/rootcause/v3"].map((dir) => new URL(`../../${dir}`, import.meta.url).pathname);

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

const lines = (root: string) =>
  files(root).flatMap((path) => readFileSync(path, "utf8").split("\n").map((line, at) => ({ where: `${path.slice(root.length + 1)}:${at + 1}`, line })));

describe("root-cause v3 names no evaluation scenario", () => {
  it.each(ROOTS)("%s names none", (root) => {
    const named = lines(root).flatMap(({ where, line }) => (SCENARIOS.test(line) ? [`${where}: ${line.trim().slice(0, 120)}`] : []));
    expect(named).toEqual([]);
  });
});
