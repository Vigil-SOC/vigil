import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Every directory tsconfig.json leaves out escapes the strict typecheck, so the list is held
// here. The root-cause v3 engine is out until it compiles with noUncheckedIndexedAccess.
const ALLOWED = ["node_modules", "plugins/rootcause/v3", "tests/rootcause/v3"];

const config = (name: string): { exclude?: string[]; include?: string[]; compilerOptions?: Record<string, unknown> } =>
  // A tsconfig is JSON with comments; these carry whole-line ones only.
  JSON.parse(readFileSync(new URL(`../../${name}`, import.meta.url), "utf8").replace(/^\s*\/\/.*$/gm, ""));

describe("tsconfig exclusions", () => {
  it("leaves out only the root-cause v3 engine", () => {
    expect(config("tsconfig.json").exclude).toEqual(ALLOWED);
  });
  it("checks what it leaves out under the base config with only one flag relaxed", () => {
    const v3 = config("tsconfig.rootcause-v3.json");
    expect(v3.compilerOptions).toEqual({ noUncheckedIndexedAccess: false });
    expect(v3.include).toEqual(["plugins/rootcause/v3/**/*.ts", "tests/rootcause/v3/**/*.ts"]);
  });
  it("typechecks both configs", () => {
    const pkg = JSON.parse(readFileSync(new URL("../../package.json", import.meta.url), "utf8")) as { scripts: Record<string, string> };
    expect(pkg.scripts["typecheck"]).toContain("-p tsconfig.rootcause-v3.json");
  });
});
