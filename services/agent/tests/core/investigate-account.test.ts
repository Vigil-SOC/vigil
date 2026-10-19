import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { headlineOf, investigateAccount } from "../../plugins/investigate/account.js";

// An investigation's account, read off the ledger: the answer, the record it kept (which
// it journaled and nothing showed), and its steps.

type Row = { kind: string; payload: unknown };

const RECORDED = readFileSync(join(import.meta.dirname, "..", "fixtures", "ledgers", "kernel-investigate.jsonl"), "utf8")
  .trim()
  .split("\n")
  .map((line, seq) => {
    const row = JSON.parse(line) as Row & { ts?: string };
    return { kind: row.kind, payload: row.payload, ts: row.ts ?? "2026-09-02T00:00:00Z", seq };
  });
const RUN = "c3468a58-80c9-4954-b0ca-d5c025727d7f";

function ledger(rows: readonly Row[]) {
  return rows.map((row, seq) => ({ ...row, ts: "2026-09-02T00:00:00Z", seq }));
}

const served = (ref: string, turn: number, source: string, thought?: string) => ({
  kind: "call",
  payload: { ref, turn, call_id: ref, tool: "splunk_search", arguments: "{}", ok: true, failure: null, class: "served", result: `<vigil:tool_result tool="splunk_search" ref="${ref}">\n3 row(s) from ${source}\n[]\n</vigil:tool_result>`, ...(thought === undefined ? {} : { thought }) },
});

describe("a recorded investigation's account", () => {
  const account = investigateAccount(RUN, RECORDED);

  it("leads with the verdict the guard let stand, and the answer's summary", () => {
    expect(account.runs).toEqual([{ run_id: RUN, run_kind: "investigate" }]);
    expect(account.verdict).toMatchObject({ disposition: "benign", confidence: "high", headline: expect.stringMatching(/^EDR detected credential dumping/) });
    expect(account.verdict).not.toHaveProperty("basis");
    // A long summary leads with its opening and keeps the whole as what happened.
    expect(account.narrative?.summary).toMatch(/^EDR detected credential dumping[^]*was run\.$/);
    expect(account.narrative?.what_happened).toContain("authorized red-team engagement");
  });

  it("shows the record it kept: every claim, which side it argues, and how it stands", () => {
    expect(account.explanations?.map((one) => [one.id, one.standing])).toEqual([
      ["h1", "weakened"],
      ["h2", "open"],
      ["h3", "supported"],
    ]);
    expect(account.explanations?.every((one) => one.run_id === RUN)).toBe(true);
  });

  it("makes each link an observation that cites its result and bears on its claim", () => {
    const first = account.evidence?.[0];
    expect(first).toMatchObject({ id: "r6.0", source_ref: "r2", turn: 1, bears_on: [{ on: "h1", stance: "supports" }] });
    expect(account.evidence?.map((one) => one.id)).toEqual(["r6.0", "r6.1", "r6.2", "r10.0", "r10.1"]);
  });

  it("ends with its steps, one per turn and the answer last, and the next steps it gave", () => {
    expect(account.steps?.at(-1)).toMatchObject({ kind: "CONCLUDE", refs: ["r2", "r3", "r9"] });
    expect(account.steps?.[0]).toMatchObject({ n: 1, kind: "EXAMINE" });
    expect(account.next_steps).toHaveLength(1);
    expect(account.gaps).toEqual([]);
  });

  it("holds no null anywhere", () => {
    expect(JSON.stringify(account)).not.toContain("null");
  });
});

describe("what the record now says outright", () => {
  const rows: Row[] = [
    { kind: "run", payload: { run_kind: "investigate" } },
    { kind: "hypothesis", payload: { hypothesis_id: "h1", statement: "adam.hartley's account dumped lsass", status: "active", provenance: "agent", resolution_reason: null, benign: false } },
    { kind: "hypothesis", payload: { hypothesis_id: "h2", statement: "An admin ran a sanctioned backup", status: "active", provenance: "agent", resolution_reason: null, benign: true } },
    // Written by a link_evidence call the run died before journaling.
    { kind: "link", payload: { evidence_id: "r0", hypothesis_id: "h2", relation: "neither", note: "orphaned" } },
    served("r1", 1, "crowdstrike:falcon:edr", "trace the process tree"),
    { kind: "link", payload: { evidence_id: "r1", hypothesis_id: "h1", relation: "supports", note: "comsvcs MiniDump of lsass on ACCT-WS-007" } },
    { kind: "call", payload: { ref: "r2", turn: 2, call_id: "r2", tool: "link_evidence", arguments: '{"links":[{"evidence_id":"r1","hypothesis_id":"h1","stance":"supports","note":"comsvcs MiniDump of lsass on ACCT-WS-007"}]}', ok: true, failure: null, class: "served", result: "linked" } },
    { kind: "question", payload: { question_id: "q1", question: "Who approved the backup window?", status: "open", hypothesis_id: "h2" } },
    {
      kind: "conclusion",
      payload: {
        plugin: "investigate",
        value: {
          summary: "Credential theft on ACCT-WS-007. It is not the backup job.",
          disposition: "benign",
          confidence: "medium",
          citations: ["r1"],
          gaps: ["Who approved the backup window?"],
          next_steps: ["Isolate ACCT-WS-007"],
          timeline: [{ at: "2026-09-02T09:12:00Z", stage: "CREDENTIAL ACCESS", what: "lsass dumped on ACCT-WS-007", techniques: ["T1003.001"], citations: ["r1"] }, { what: "okta queried", citations: ["r3"] }],
        },
        capped: false,
        turns: 2,
        cost_usd: 0.1,
        declared_gaps: { open_questions: ["Who approved the backup window?"], declared: [], visibility: [{ ref: "r3", tool: "okta_logs", failure: "timeout" }] },
      },
    },
    { kind: "review", payload: { stance: "insufficient", reasons: "one source only", missing: [], cited: ["r1"], unknown_refs: [], guarded: { field: "disposition", asked: "benign", final: "needs_review" } } },
  ];
  const account = investigateAccount("run-2", ledger(rows));

  it("says what the guard held the answer as, and why", () => {
    expect(account.verdict).toMatchObject({ disposition: "needs_review", confidence: "medium", headline: "Credential theft on ACCT-WS-007." });
    expect(account.verdict?.basis).toContain("asked for benign");
  });

  it("takes each claim's side and each observation's words from the record", () => {
    expect(account.explanations?.map((one) => [one.id, one.role, one.standing])).toEqual([
      ["h1", "adversary", "supported"],
      ["h2", "benign", "open"],
    ]);
    expect(account.evidence).toEqual([
      { id: "r2.0", run_id: "run-2", claim: "comsvcs MiniDump of lsass on ACCT-WS-007", source: "crowdstrike:falcon:edr", turn: 1, source_ref: "r1", bears_on: [{ on: "h1", stance: "supports" }] },
    ]);
  });

  it("lays the timeline out from the answer, pointed at the observation it cites", () => {
    expect(account.timeline).toEqual([
      { run_id: "run-2", text: "lsass dumped on ACCT-WS-007", at: "2026-09-02T09:12:00Z", stage: "CREDENTIAL ACCESS", techniques: ["T1003.001"], ref: "r2.0" },
      { run_id: "run-2", text: "okta queried" },
    ]);
  });

  it("counts only the turns that asked the estate something as steps", () => {
    expect(account.steps?.map((step) => [step.kind, step.refs])).toEqual([
      ["EXAMINE", ["r1"]],
      ["CONCLUDE", ["r1"]],
    ]);
  });

  it("names a question once, and a call the estate failed as a blind spot", () => {
    expect(account.gaps).toEqual([
      { run_id: "run-2", text: "Who approved the backup window?", kind: "open_question" },
      { run_id: "run-2", text: "okta_logs could not answer r3 (timeout)", kind: "visibility" },
    ]);
  });

  it("takes a shouted label off a headline, and only a label", () => {
    expect(headlineOf("CONFIRMED MALICIOUS — Credential theft on ACCT-WS-007. More.")).toBe("Credential theft on ACCT-WS-007.");
    expect(headlineOf("VERDICT: benign backup job.")).toBe("Benign backup job.");
    expect(headlineOf("SSH-based lateral movement from bastion-01. More.")).toBe("SSH-based lateral movement from bastion-01.");
    expect(headlineOf("US-EAST-1 instance i-0abc launched a miner.")).toBe("US-EAST-1 instance i-0abc launched a miner.");
    expect(headlineOf("The user ran tools, e.g. procdump, on the host. More.")).toBe("The user ran tools, e.g. procdump, on the host.");
  });

  it("opens a listed or labelled summary on its first sentences, not on a list marker", () => {
    const listed = "Findings:\n1. Phishing mail delivered a macro. 2. It ran.\n\nThe host beaconed.";
    expect(headlineOf(listed)).toBe("Findings: Phishing mail delivered a macro.");
    expect(headlineOf("Findings: 1. Beacon on port 443. 2. It ran.")).toBe("Findings: Beacon on port 443.");
    const narrated = (summary: string) =>
      investigateAccount("run-5", ledger([{ kind: "conclusion", payload: { plugin: "investigate", value: { summary }, capped: false, turns: 1, cost_usd: 0 } }])).narrative;
    expect(narrated(listed)).toEqual({ run_id: "run-5", summary: "Findings: Phishing mail delivered a macro. It ran.", what_happened: listed });
    expect(narrated("CONFIRMED MALICIOUS — Credential theft. On ACCT-WS-007.")).toEqual({ run_id: "run-5", summary: "Credential theft. On ACCT-WS-007." });
  });

  it("reads an older claim's side off its opening words", () => {
    const older = investigateAccount(
      "run-3",
      ledger([{ kind: "hypothesis", payload: { hypothesis_id: "h1", statement: "Benign administrative activity by IT", status: "active", provenance: "agent", resolution_reason: null } }]),
    );
    expect(older.explanations?.[0]?.role).toBe("benign");
    const said = investigateAccount(
      "run-4",
      ledger([{ kind: "hypothesis", payload: { hypothesis_id: "h1", statement: "This is an authorized pentest", status: "active", provenance: "agent", resolution_reason: null } }]),
    );
    expect(said.explanations?.[0]?.role).toBe("benign");
    expect(older).not.toHaveProperty("verdict");
    expect(older).not.toHaveProperty("gaps");
  });
});
