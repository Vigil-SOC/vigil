import { describe, expect, it } from "vitest";
import { schemaErrors } from "../../../core/stream.js";
import { salvage } from "../../../plugins/rootcause/v3/model/client.js";
import { SUBMIT, type SubmitName } from "../../../plugins/rootcause/v3/model/schemas.js";
import { splunkTool } from "../../../plugins/rootcause/v3/tools/splunk-tool.js";

// The schemas are written by hand as JSON Schema; they have to compile under the agent's ajv (core/stream.ts),
// the one validator that holds a model's emission and a search's input to them.
const backend = { search: async () => "", count: async () => 0 };
const search = splunkTool(backend, "lab").inputSchema;
const names = Object.keys(SUBMIT) as SubmitName[];
const cand = { label: "curl wrote /tmp/x", domain: "edr", prior: 0.3, artifacts: [], nodeEvidence: [], edgeEvidence: [] };
const causes = { candidates: [cand], reasoning: "r" };

describe("v3 tool schemas", () => {
  it.each(names)("%s compiles under the agent's ajv", (name) => {
    expect(() => schemaErrors(SUBMIT[name], {})).not.toThrow();
  });
  it("the search tool compiles under the agent's ajv", () => {
    expect(() => schemaErrors(search, {})).not.toThrow();
  });
  it("names no draft, so a reader does not pick a different one", () => {
    for (const name of names) expect(SUBMIT[name]).not.toHaveProperty("$schema");
  });
  it("accepts a well-formed submission", () => {
    expect(schemaErrors(SUBMIT.submit_causes, causes)).toBeNull();
  });
  it("rejects a submission missing a required field at the path ajv reports to the kernel", () => {
    const { label: _, ...noLabel } = cand;
    expect(schemaErrors(SUBMIT.submit_causes, { ...causes, candidates: [noLabel] })).toContain("/candidates/0 must have required property 'label'");
    expect(schemaErrors(SUBMIT.submit_causes, { candidates: [cand] })).toContain("/ must have required property 'reasoning'");
  });
  it("leaves relation optional on an identity signal", () => {
    const signal = { layer: "machine-facing", kind: "account", value: "svc", toolCallIds: ["r1"] };
    expect(schemaErrors(SUBMIT.submit_causes, { ...causes, candidates: [{ ...cand, identitySignals: [signal] }] })).toBeNull();
  });
  it("rejects a search without a purpose", () => {
    expect(schemaErrors(search, { spl: "index=lab sourcetype=edr" })).toContain("must have required property 'purpose'");
  });
  it("a salvage that would leave a list under its minimum fails, as the schema's item counts say", () => {
    const theory = (id: string) => ({ id, story: "s", accountsFor: [], gaps: "g", predictions: [{ id: "P1", sourcetype: "edr", expect: "e", spl: "index=lab", separates: [] }] });
    const input = { theories: [theory("H1"), theory("H2")], nextTests: ["P1"], reasoning: "r" };
    expect(salvage("submit_theories", input, 0, {}, { success: false, msg: "m", drop: [["theories", 0]] })).toBeNull();
    expect(salvage("submit_theories", { ...input, theories: [theory("H1"), theory("H2"), theory("H3")] }, 0, {}, { success: false, msg: "m", drop: [["theories", 0]] })?.dropped).toBe(1);
  });
});
