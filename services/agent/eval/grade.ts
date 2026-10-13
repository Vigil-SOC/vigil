// What a run concluded, read off its final report the same way for every engine. It judges
// what the report says, never whether it is right: score.ts compares the label.

export type Verdict = "attack" | "benign" | "undetermined";

export interface Graded {
  verdict: Verdict;
  reason: string;
  grader: string;
  cost_usd?: number;
}

export const GRADER_PROMPT = [
  "You read the final report of a security investigation and say what it concluded.",
  "attack: it concluded the activity is malicious or suspicious and should be escalated or contained.",
  "benign: it concluded the activity is benign or a false positive and can be closed.",
  "undetermined: it could not decide, held the case for human review, or said it was inconclusive.",
  "If the report states a conclusion and then says it was held for review, the answer is undetermined.",
  "Judge only what the report concludes, not whether that conclusion is correct.",
  'Answer with JSON only: {"verdict": "attack" | "benign" | "undetermined", "reason": "<one sentence>"}',
].join("\n");

export function parseVerdict(text: string): { verdict: Verdict; reason: string } | null {
  const fenced = /```(?:json)?\s*([\s\S]*?)```/.exec(text);
  const body = (fenced?.[1] ?? text).trim();
  const start = body.indexOf("{");
  const end = body.lastIndexOf("}");
  if (start === -1 || end <= start) return null;
  try {
    const parsed = JSON.parse(body.slice(start, end + 1)) as { verdict?: unknown; reason?: unknown };
    if (parsed.verdict !== "attack" && parsed.verdict !== "benign" && parsed.verdict !== "undetermined") return null;
    return { verdict: parsed.verdict, reason: typeof parsed.reason === "string" ? parsed.reason : "" };
  } catch {
    return null;
  }
}

// For a dry run only: proves the pipeline, grades nothing that matters.
export function keywordGrade(report: string): Graded {
  const text = report.toLowerCase();
  const verdict: Verdict = /needs_review|needs review|inconclusive|could not determine|undetermined/.test(text)
    ? "undetermined"
    : /malicious|suspicious|compromise|attack/.test(text)
      ? "attack"
      : /benign|false[_ ]positive/.test(text)
        ? "benign"
        : "undetermined";
  return { verdict, reason: "keyword grade (dry run)", grader: "keywords" };
}

export interface GraderClient {
  model: string;
  complete(system: string, user: string): Promise<{ text: string; cost_usd?: number }>;
}

export class GradeFailed extends Error {}

// Asked three times at most. An unparsable grade is an error, never a verdict: counted as
// undetermined it would read as a run that held the case.
export async function grade(client: GraderClient, report: string): Promise<Graded> {
  const user = `## Final report\n\n${report.trim() === "" ? "(the run left no report)" : report}`;
  let cost = 0;
  let last = "";
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const answer = await client.complete(GRADER_PROMPT, user);
    cost += answer.cost_usd ?? 0;
    last = answer.text;
    const parsed = parseVerdict(answer.text);
    if (parsed !== null) return { ...parsed, grader: client.model, cost_usd: cost };
  }
  throw new GradeFailed(`the grader did not answer in the expected shape: ${JSON.stringify(last.slice(0, 200))}`);
}
