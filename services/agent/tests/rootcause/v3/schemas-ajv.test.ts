import { Ajv } from "ajv";
import { describe, expect, it } from "vitest";
import { SUBMIT, jsonSchema, type SubmitName } from "../../../plugins/rootcause/v3/model/schemas.js";
import { splunkTool } from "../../../plugins/rootcause/v3/tools/splunk-tool.js";

// The schemas the model is offered are generated from zod; they have to be JSON Schema the
// rest of the agent reads, under the same settings as core/stream.ts.
const ajv = new Ajv({ allErrors: true, strict: false });
const backend = { search: async () => "", count: async () => 0 };

describe("v3 tool schemas", () => {
  it.each(Object.keys(SUBMIT) as SubmitName[])("%s compiles under the agent's ajv", (name) => {
    expect(() => ajv.compile(jsonSchema(SUBMIT[name]))).not.toThrow();
  });
  it("the search tool compiles under the agent's ajv", () => {
    expect(() => ajv.compile(jsonSchema(splunkTool(backend, "lab").inputSchema))).not.toThrow();
  });
  it("names no draft, so a reader does not pick a different one", () => {
    for (const name of Object.keys(SUBMIT) as SubmitName[]) expect(jsonSchema(SUBMIT[name])).not.toHaveProperty("$schema");
  });
});
