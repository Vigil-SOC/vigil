import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Every directory tsconfig.json leaves out escapes the strict typecheck, so the list is held here.
const ALLOWED = ["node_modules"];

const config = (name: string): { exclude?: string[] } =>
  // A tsconfig is JSON with comments; these carry whole-line ones only.
  JSON.parse(readFileSync(new URL(`../../${name}`, import.meta.url), "utf8").replace(/^\s*\/\/.*$/gm, ""));

describe("tsconfig exclusions", () => {
  it("leaves out nothing but node_modules", () => {
    expect(config("tsconfig.json").exclude).toEqual(ALLOWED);
  });
});
