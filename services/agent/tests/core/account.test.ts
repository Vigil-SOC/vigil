import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

// The account crosses into Python as JSON, so what the contract accepts is checked
// on JSON: each fixture is handed to the compiler as a CaseAccount literal, which
// checks its unions and refuses a field the type does not name. The pydantic mirror
// is held to the same files (tests/unit/storage/test_case_account.py).

const FIXTURES = join(import.meta.dirname, "..", "fixtures", "account");
const CONTRACTS = join(import.meta.dirname, "..", "..", "contracts");

const jsonIn = (dir: string): Map<string, string> =>
  new Map(
    readdirSync(dir)
      .filter((name) => name.endsWith(".json"))
      .map((name) => [name, readFileSync(join(dir, name), "utf8")]),
  );

const VALID = jsonIn(FIXTURES);
const INVALID = jsonIn(join(FIXTURES, "invalid"));

// One program over every fixture, each its own file beside the contract, so a
// diagnostic names the fixture it came from.
function typeErrors(fixtures: ReadonlyMap<string, string>): Map<string, string[]> {
  const options: ts.CompilerOptions = {
    strict: true,
    noEmit: true,
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    lib: ["lib.es2023.d.ts"],
    types: [],
  };
  const sources = new Map(
    [...fixtures].map(([name, json]) => [
      join(CONTRACTS, `fixture.${name.replaceAll("/", "-")}.ts`),
      `import type { CaseAccount } from "./account.js";\nexport const account: CaseAccount = ${json};\n`,
    ]),
  );
  const host = ts.createCompilerHost(options);
  const read = host.getSourceFile.bind(host);
  host.getSourceFile = (name, language, ...rest) => {
    const text = sources.get(name);
    return text === undefined ? read(name, language, ...rest) : ts.createSourceFile(name, text, language);
  };
  const exists = host.fileExists.bind(host);
  host.fileExists = (name) => sources.has(name) || exists(name);
  const program = ts.createProgram([...sources.keys()], options, host);
  return new Map(
    [...sources.keys()].map((path, at) => [
      [...fixtures.keys()][at] as string,
      ts.getPreEmitDiagnostics(program, program.getSourceFile(path)).map((one) => ts.flattenDiagnosticMessageText(one.messageText, "\n")),
    ]),
  );
}

describe("the account contract on JSON", () => {
  const errors = typeErrors(new Map([...VALID, ...[...INVALID].map(([name, json]) => [`invalid/${name}`, json] as const)]));

  it("has fixtures to hold it to", () => {
    expect(VALID.size).toBeGreaterThan(0);
    expect(INVALID.size).toBeGreaterThan(0);
  });

  it.each([...VALID.keys()])("accepts %s", (name) => {
    expect(errors.get(name)).toEqual([]);
  });

  // Refused by the type, not by a fixture that failed to reach it.
  it.each([...INVALID.keys()])("refuses invalid/%s", (name) => {
    const refused = errors.get(`invalid/${name}`) ?? [];
    expect(refused.length).toBeGreaterThan(0);
    expect(refused.filter((one) => one.includes("Cannot find module"))).toEqual([]);
  });
});
