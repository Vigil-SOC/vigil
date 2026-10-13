import { readFileSync, writeFileSync } from "node:fs";
import { parseArgs } from "node:util";
import OpenAI from "openai";
import { grade, type GraderClient } from "./grade.js";

// Grades the reports a run already left behind again, in place, for a failed grader or a new one:
// npx tsx eval/regrade.ts eval/results/baseline.jsonl eval/results/kernel.jsonl [--grader-model vertex/gemini-2.5-flash] [--failed-only]

const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: { "grader-model": { type: "string", default: "vertex/gemini-2.5-flash" }, "failed-only": { type: "boolean", default: false } },
});
const BIFROST = (process.env["BIFROST_URL"] ?? "http://127.0.0.1:8080").replace(/\/+$/, "");
const model = values["grader-model"]!;
const client = new OpenAI({ baseURL: `${BIFROST}/v1`, apiKey: process.env["BIFROST_API_KEY"] ?? "unused", maxRetries: 3 });
const grader: GraderClient = {
  model,
  complete: async (system, user) => {
    const response = await client.chat.completions.create({
      model,
      max_tokens: 2_000,
      messages: [
        { role: "system", content: system },
        { role: "user", content: user },
      ],
    });
    return { text: response.choices[0]?.message?.content ?? "" };
  },
};

for (const file of positionals) {
  const rows = readFileSync(file, "utf8").split("\n").filter((line) => line.trim() !== "").map((line) => JSON.parse(line) as Record<string, unknown>);
  for (const row of rows) {
    const failed = typeof row["error"] === "string" && (row["error"] as string).startsWith("grading failed");
    // A run that never reached a terminal has nothing to grade.
    if (row["outcome"] === null || (values["failed-only"] && !failed)) continue;
    try {
      const graded = await grade(grader, String(row["report"] ?? ""));
      Object.assign(row, { verdict: graded.verdict, grade_reason: graded.reason, ...(failed ? { error: null } : {}) });
    } catch (error) {
      Object.assign(row, { verdict: null, error: `grading failed: ${(error as Error).message}` });
    }
    console.log(`${file}: ${String(row["case_id"]).padEnd(36)} ${row["verdict"] ?? "ERROR"}`);
  }
  writeFileSync(file, `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`);
}
