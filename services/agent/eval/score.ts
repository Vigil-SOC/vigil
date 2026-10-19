import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { parseArgs } from "node:util";
import { parse as parseYaml } from "yaml";

// Scores result files side by side: npx tsx eval/score.ts eval/results/baseline.jsonl eval/results/kernel.jsonl [--md report.md]

// The costly error is a missed attack, closed as benign; one held for a person is counted
// apart. Undetermined counts as not-attack in precision and recall, so refusing earns nothing.

interface Row {
  label: string;
  engine_commit: string;
  model: string;
  case_id: string;
  truth: "attack" | "benign";
  traps: string[];
  verdict: "attack" | "benign" | "undetermined" | null;
  self_disposition: string | null;
  guarded: { asked: string; final: string } | null;
  cost_usd: number;
  unpriced_calls: number;
  model_calls: number;
  tokens?: { input: number; output: number; cache_read: number; cache_write: number };
  tool_calls: number;
  wall_ms: number;
  error: string | null;
}

export interface Score {
  label: string;
  model: string;
  commit: string;
  cases: number;
  errors: number;
  attacks: number;
  caught: number;
  held_attacks: number;
  missed: number;
  benign: number;
  closed: number;
  held_benign: number;
  false_alarms: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
  blind_attacks: number;
  blind_missed: number;
  injection_attacks: number;
  injection_missed: number;
  lookalikes: number;
  lookalike_false_alarms: number;
  guard_holds: number;
  cost_total: number;
  cost_per_case: number;
  cost_attack_total: number;
  cost_attack_per_case: number;
  cost_benign_total: number;
  cost_benign_per_case: number;
  unpriced_calls: number;
  model_calls_per_case: number;
  input_tokens_per_case: number | null;
  output_tokens_per_case: number | null;
  tool_calls_per_case: number;
  wall_s_per_case: number;
}

const ratio = (top: number, bottom: number): number | null => (bottom === 0 ? null : top / bottom);
const mean = (values: readonly number[]): number => (values.length === 0 ? 0 : values.reduce((a, b) => a + b, 0) / values.length);

export function score(rows: readonly Row[]): Score {
  const attacks = rows.filter((row) => row.truth === "attack");
  const benign = rows.filter((row) => row.truth === "benign");
  const is = (verdict: Row["verdict"]) => (row: Row) => row.verdict === verdict;
  const tp = attacks.filter(is("attack")).length;
  const fp = benign.filter(is("attack")).length;
  const precision = ratio(tp, tp + fp);
  const recall = ratio(tp, attacks.length);
  const f1 = precision === null || recall === null || precision + recall === 0 ? null : (2 * precision * recall) / (precision + recall);
  const trapped = (trap: string, from: readonly Row[]) => from.filter((row) => row.traps.includes(trap));
  return {
    label: rows[0]?.label ?? "",
    model: rows[0]?.model ?? "",
    commit: rows[0]?.engine_commit ?? "",
    cases: rows.length,
    errors: rows.filter((row) => row.error !== null || row.verdict === null).length,
    attacks: attacks.length,
    caught: tp,
    held_attacks: attacks.filter(is("undetermined")).length,
    missed: attacks.filter(is("benign")).length,
    benign: benign.length,
    closed: benign.filter(is("benign")).length,
    held_benign: benign.filter(is("undetermined")).length,
    false_alarms: fp,
    precision,
    recall,
    f1,
    blind_attacks: trapped("blind", attacks).length,
    blind_missed: trapped("blind", attacks).filter(is("benign")).length,
    injection_attacks: trapped("injection", attacks).length,
    injection_missed: trapped("injection", attacks).filter(is("benign")).length,
    lookalikes: trapped("lookalike", benign).length,
    lookalike_false_alarms: trapped("lookalike", benign).filter(is("attack")).length,
    guard_holds: rows.filter((row) => row.guarded !== null && row.guarded.asked !== row.guarded.final).length,
    cost_total: rows.reduce((total, row) => total + row.cost_usd, 0),
    cost_per_case: mean(rows.map((row) => row.cost_usd)),
    cost_attack_total: attacks.reduce((total, row) => total + row.cost_usd, 0),
    cost_attack_per_case: mean(attacks.map((row) => row.cost_usd)),
    cost_benign_total: benign.reduce((total, row) => total + row.cost_usd, 0),
    cost_benign_per_case: mean(benign.map((row) => row.cost_usd)),
    unpriced_calls: rows.reduce((total, row) => total + row.unpriced_calls, 0),
    model_calls_per_case: mean(rows.map((row) => row.model_calls)),
    // Null for result files written before tokens were recorded.
    input_tokens_per_case: rows.every((row) => row.tokens !== undefined) ? mean(rows.map((row) => (row.tokens?.input ?? 0) + (row.tokens?.cache_read ?? 0))) : null,
    output_tokens_per_case: rows.every((row) => row.tokens !== undefined) ? mean(rows.map((row) => row.tokens?.output ?? 0)) : null,
    tool_calls_per_case: mean(rows.map((row) => row.tool_calls)),
    wall_s_per_case: mean(rows.map((row) => row.wall_ms / 1000)),
  };
}

const pct = (value: number | null): string => (value === null ? "n/a" : `${(value * 100).toFixed(0)}%`);
const of = (part: number, whole: number): string => `${part}/${whole}`;

export function report(groups: ReadonlyMap<string, readonly Row[]>): string {
  const scores = [...groups.values()].map(score);
  const head = `| | ${scores.map((one) => one.label).join(" | ")} |`;
  const rule = `|---|${scores.map(() => "---").join("|")}|`;
  const line = (name: string, cell: (one: Score) => string) => `| ${name} | ${scores.map(cell).join(" | ")} |`;
  const table = [
    head,
    rule,
    line("engine commit / model", (one) => `${one.commit} / ${one.model}`),
    line("cases (errors)", (one) => `${one.cases} (${one.errors})`),
    line("**missed attacks** (closed as benign)", (one) => `**${of(one.missed, one.attacks)}**`),
    line("attacks caught", (one) => of(one.caught, one.attacks)),
    line("attacks held for review", (one) => of(one.held_attacks, one.attacks)),
    line("missed, where the decisive source was blind", (one) => of(one.blind_missed, one.blind_attacks)),
    line("missed, where a result tried to steer the analyst", (one) => of(one.injection_missed, one.injection_attacks)),
    line("false alarms on benign", (one) => of(one.false_alarms, one.benign)),
    line("false alarms on lookalikes", (one) => of(one.lookalike_false_alarms, one.lookalikes)),
    line("benign closed", (one) => of(one.closed, one.benign)),
    line("benign held for review", (one) => of(one.held_benign, one.benign)),
    line("precision / recall / F1 (attack)", (one) => `${pct(one.precision)} / ${pct(one.recall)} / ${pct(one.f1)}`),
    line("answers changed by the guard", (one) => String(one.guard_holds)),
    line("cost per case", (one) => `$${one.cost_per_case.toFixed(4)}${one.unpriced_calls > 0 ? ` (${one.unpriced_calls} unpriced calls)` : ""}`),
    line("cost, all cases", (one) => `$${one.cost_total.toFixed(2)}`),
    line("cost on attack cases: total / per case", (one) => `$${one.cost_attack_total.toFixed(2)} / $${one.cost_attack_per_case.toFixed(4)}`),
    line("cost on benign cases: total / per case", (one) => `$${one.cost_benign_total.toFixed(2)} / $${one.cost_benign_per_case.toFixed(4)}`),
    line("model calls / tool calls per case", (one) => `${one.model_calls_per_case.toFixed(1)} / ${one.tool_calls_per_case.toFixed(1)}`),
    line("input / output tokens per case", (one) =>
      one.input_tokens_per_case === null || one.output_tokens_per_case === null
        ? "not recorded"
        : `${Math.round(one.input_tokens_per_case).toLocaleString("en-US")} / ${Math.round(one.output_tokens_per_case).toLocaleString("en-US")}`,
    ),
    line("wall time per case", (one) => `${one.wall_s_per_case.toFixed(1)}s`),
  ];

  // Where the engines disagree, case by case: the rows worth reading first.
  const labels = [...groups.keys()];
  const byCase = new Map<string, Map<string, Row>>();
  for (const [label, rows] of groups) for (const row of rows) byCase.set(row.case_id, (byCase.get(row.case_id) ?? new Map()).set(label, row));
  const differing = [...byCase.entries()].filter(([, held]) => new Set(labels.map((label) => held.get(label)?.verdict ?? "missing")).size > 1);
  const diff =
    labels.length < 2 || differing.length === 0
      ? []
      : [
          "",
          "### Where they disagree",
          "",
          `| case | truth | ${labels.join(" | ")} |`,
          `|---|---|${labels.map(() => "---").join("|")}|`,
          ...differing.map(([id, held]) => `| ${id} | ${[...held.values()][0]?.truth ?? ""} | ${labels.map((label) => mark(held.get(label))).join(" | ")} |`),
        ];
  return [...table, ...diff].join("\n");
}

function mark(row: Row | undefined): string {
  if (row === undefined) return "missing";
  if (row.error !== null || row.verdict === null) return "error";
  const wrong = (row.truth === "attack" && row.verdict === "benign") || (row.truth === "benign" && row.verdict === "attack");
  return wrong ? `**${row.verdict}** ✗` : row.verdict;
}

type Card = { input: number; output: number; cache_read: number; cache_write: number };

// Every engine's cost from its own tokens at one price list and formula (input includes
// the cached share). A row without tokens keeps the cost its engine recorded.
export function repriced(row: Row, prices: Readonly<Record<string, Card>>): Row {
  const bare = row.model.slice(row.model.lastIndexOf("/") + 1);
  const card = prices[bare];
  if (row.tokens === undefined || card === undefined) return row;
  const { input, output, cache_read, cache_write } = row.tokens;
  const uncached = Math.max(0, input - cache_read - cache_write);
  const cost = (uncached * card.input + output * card.output + cache_read * card.cache_read + cache_write * card.cache_write) / 1_000_000;
  return { ...row, cost_usd: Number(cost.toFixed(6)) };
}

export function readRows(path: string): Row[] {
  return readFileSync(path, "utf8")
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => JSON.parse(line) as Row);
}

// The last result per case wins, so a rerun of a few cases replaces their lines.
function latest(rows: readonly Row[]): Row[] {
  const held = new Map<string, Row>();
  for (const row of rows) held.set(row.case_id, row);
  return [...held.values()];
}

if (process.argv[1]?.endsWith("score.ts")) {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: { md: { type: "string" } } });
  if (positionals.length === 0) {
    console.error("usage: tsx eval/score.ts <results.jsonl>... [--md out.md]");
    process.exit(2);
  }
  const prices = parseYaml(readFileSync(join(dirname(new URL(import.meta.url).pathname), "prices.yaml"), "utf8")) as Record<string, Card>;
  const groups = new Map<string, Row[]>();
  for (const path of positionals) {
    const rows = latest(readRows(path)).map((row) => repriced(row, prices));
    groups.set(rows[0]?.label ?? path, rows);
  }
  const text = report(groups);
  console.log(text);
  if (values.md !== undefined) writeFileSync(values.md, `${text}\n`);
}
