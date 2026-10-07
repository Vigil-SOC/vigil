import { describe, expect, it } from "vitest";
import { narrativeInput, renderNarrative, type Narrative } from "../../workflows/hunt/narrative.js";
import { buildReport, renderReport } from "../../workflows/hunt/report.js";
import { newLedger } from "../support/hunt.js";

const WRITTEN: Narrative = {
  summary: "Two unrelated things happened, not one.",
  what_happened: "## Incident 1\nA host beaconed to 45.77.53.176.\n\n## Incident 2\nA key leaked to a public repo.",
  next_steps: ["Isolate FYODOR-L — it holds the live beacon", "Revoke the leaked key — it is still valid"],
  model_id: "vertex/gemini-3.5-flash",
  written_at: "2026-08-20T19:00:00.000Z",
  cost_usd: 0.01,
};

describe("the account a person reads first", () => {
  it("leads with the summary, then what happened, then what to do", () => {
    const rendered = renderNarrative(WRITTEN).join("\n");
    expect(rendered.indexOf("## What happened")).toBeLessThan(rendered.indexOf("## What to do now"));
    expect(rendered).toContain("Two unrelated things happened");
    expect(rendered).toContain("1. Isolate FYODOR-L — it holds the live beacon");
    expect(rendered).toContain("2. Revoke the leaked key");
  });

  it("says so rather than inventing an action when the record supports none", () => {
    const rendered = renderNarrative({ ...WRITTEN, next_steps: [] }).join("\n");
    expect(rendered).toContain("Nothing here supports an action on its own");
  });

  it("names what wrote it, so a reader can tell the account from the record", () => {
    expect(renderNarrative(WRITTEN).join("\n")).toContain("vertex/gemini-3.5-flash");
  });
});

// The schema asked for objects before it asked for strings, and the ledger holds
// what it holds: a rendering that reads only the current shape prints
// "[object Object]" over every account written before the change.
describe("an account written under the older shape still renders", () => {
  it("reads an {action, why} step the ledger already holds", () => {
    const held = { ...WRITTEN, next_steps: [{ action: "Isolate FYODOR-L", why: "it holds the live beacon" }] } as unknown as Narrative;
    const rendered = renderNarrative(held).join("\n");
    expect(rendered).toContain("1. Isolate FYODOR-L — it holds the live beacon");
    expect(rendered).not.toContain("[object Object]");
  });

  it("reads an action with no reason beside it", () => {
    const held = { ...WRITTEN, next_steps: [{ action: "Revoke the key" }] } as unknown as Narrative;
    expect(renderNarrative(held).join("\n")).toContain("1. Revoke the key");
  });

  it("drops a step it cannot read rather than guessing at one", () => {
    const held = { ...WRITTEN, next_steps: [42, { why: "no action" }, "Isolate the host"] } as unknown as Narrative;
    const rendered = renderNarrative(held).join("\n");
    expect(rendered).toContain("1. Isolate the host");
    expect(rendered).not.toContain("42");
  });
});

describe("the report carries the account without depending on one", () => {
  it("puts the account above the verdicts", async () => {
    const { ledger } = await newLedger({ hypotheses: ["a host beaconed out"] });
    const report = buildReport(ledger.projection);
    const rendered = renderReport(report, ledger.projection, WRITTEN);
    expect(rendered.indexOf("## What happened")).toBeLessThan(rendered.indexOf("## Verdicts"));
  });

  // Fail-open: the verdicts and findings are the deliverable, so a narrator that
  // could not run costs the report its opening section and nothing else.
  it("renders the whole report unchanged when nothing wrote an account", async () => {
    const { ledger } = await newLedger({ hypotheses: ["a host beaconed out"] });
    const report = buildReport(ledger.projection);
    expect(renderReport(report, ledger.projection, null)).toBe(renderReport(report, ledger.projection));
    expect(renderReport(report, ledger.projection)).toContain("## Verdicts");
  });
});

describe("what the narrator is given", () => {
  it("hands over the record under headings the writer can reason about", async () => {
    const { ledger } = await newLedger({ hypotheses: ["a host beaconed out"] });
    const input = narrativeInput(ledger.projection, buildReport(ledger.projection));
    expect(input).toContain("## How each belief stands");
    expect(input).toContain("## What could not be seen");
    expect(input).toContain("## Handed to incident response");
    expect(input).toContain("a host beaconed out");
  });

  // An account written off one-line summaries can only restate the summaries; the
  // addresses and filenames a reader needs live in the payload.
  it("carries a record's payload, not only its summary", () => {
    const projection = {
      evidence: new Map([["ev-1", { evidence_id: "ev-1", iteration: 1, summary: "outbound traffic", source_system: "net_flow", salience: "anomalous", payload: { dest: "45.77.53.176", port: 3333 } }]]),
      links: [],
    } as unknown as Parameters<typeof narrativeInput>[0];
    const report = { hunt_id: "h", name: "n", outcome: null, reason: "", iterations: 1, hypotheses: [], gaps: [], handoffs: [] } as unknown as Parameters<typeof narrativeInput>[1];
    const input = narrativeInput(projection, report);
    expect(input).toContain("45.77.53.176");
    expect(input).toContain("3333");
  });
});

