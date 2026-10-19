export interface AgentConfig {
  model: string;
  maxTurns: number;
  maxTotalTokens: number;
  maxWallClockMinutes: number;
  maxUsd: number;
  priceInPerM: number;
  priceOutPerM: number;
  maxToolCallsPerTurn: number;
  strengthenSearches: number;
  scopeModel: string;
  refusalFallbackModel: string;
  scopePriceScale: number;
  maxTheorize: number;
  rareSweep: boolean;
  theorizeEvery: number;
  branchEvery: number;
  rawOutputMaxChars: number;
  toolTimeoutMs: number;
  maxStrengthenAttempts: number;
  maxExpandAttempts: number;
  stallDelta: number;
  maxForksPerNode: number;
  maxForwardSteps: number;
  lookbackDays: number;
  stitchWindowMinutes: number;
  linkNeedsSharedValue: boolean;
  roleArnHandOff: boolean;
  inheritLegit: boolean;
  lead: boolean;
  loose: boolean; // record freely: findings that fail a check are tagged (unverified / inferred link), not refused; the report is strict
  wrapUpAt: number; // share of maxUsd (or of maxTurns) left when the model is told to wrap up
  concludeAt: number; // share left when the run stops for the closing conclude turn
  thresholds: { confirm: number; settled: number; ruleOut: number; expandMin: number; altPathMin: number; competitorMax: number };
  weights: { direct: number; corroborating: number; circumstantial: number };
  priorRange: [number, number];
  concurrency: number;
  runsDir: string;
}

export const defaultConfig: AgentConfig = {
  model: (process.env.MODEL ?? "claude-opus-4-8").replace(/^vertex-anthropic\//, ""),
  maxTurns: 40,
  maxTotalTokens: 2_000_000,
  maxWallClockMinutes: 30,
  maxUsd: Number(process.env.BUDGET_USD ?? 7),
  priceInPerM: Number(process.env.PRICE_IN_PER_M ?? 5),
  priceOutPerM: Number(process.env.PRICE_OUT_PER_M ?? 25),
  maxToolCallsPerTurn: 6,
  // Environment mapping is broad, plain aggregation: a cheaper model does it (price relative to the main model).
  scopeModel: (process.env.SCOPE_MODEL ?? "claude-sonnet-5").replace(/^vertex-anthropic\//, ""),
  scopePriceScale: Number(process.env.SCOPE_PRICE_SCALE ?? 0.6),
  refusalFallbackModel: (process.env.REFUSAL_FALLBACK_MODEL ?? "claude-sonnet-5").replace(/^vertex-anthropic\//, ""),
  strengthenSearches: 3, // a check needs a record or two; 6 let strengthen take a third of all searches
  rareSweep: true, // rare-activity sweep at the start of a run (rare.ts)
  maxTheorize: 8, // rounds of competing stories per run (4 ran out by mid-run with the key question open)
  theorizeEvery: 4, // turns of other moves between rounds, once a round's tests are done
  branchEvery: 6, // once the stories run, a branch from a chain event at least this often
  rawOutputMaxChars: Number(process.env.RAW_OUTPUT_MAX_CHARS ?? 20_000), // v1 found 8k too small for 50 rows; session runs use 8k to cut cost
  toolTimeoutMs: 90_000, // big sources (millions of events) need it; a slow search returns partial results
  maxStrengthenAttempts: 3,
  maxExpandAttempts: 2,
  stallDelta: 0.05,
  maxForksPerNode: 3,
  maxForwardSteps: 3, // entry + 2 forward steps (a 5-step pivot walked a developer's routine CI)
  lookbackDays: 14,
  stitchWindowMinutes: 30,
  linkNeedsSharedValue: true, // a new cause/effect link needs a value both events carry
  inheritLegit: process.env.ROLE_ARN_HANDOFF !== "0", // a step causing its own account's legitimate step is legitimate; off with the A/B env
  roleArnHandOff: process.env.ROLE_ARN_HANDOFF !== "0", // role ARNs join different accounts only as a hand-off; ROLE_ARN_HANDOFF=0 for the mapping A/B
  wrapUpAt: 0.10,
  concludeAt: 0.03, // at least $0.50: one conclude turn costs about that
  loose: process.env.LOOSE === "1",
  lead: process.env.LEAD !== "0", // the model names each next move (lead.ts); LEAD=0 = v2's frontier only
  thresholds: { confirm: 0.85, settled: 0.95, ruleOut: 0.1, expandMin: 0.6, altPathMin: 0.3, competitorMax: 0.15 },
  weights: { direct: 2.0, corroborating: 1.0, circumstantial: 0.4 },
  priorRange: [0.1, 0.5],
  concurrency: 1,
  runsDir: "runs",
};
