// The throwaway workflow: two verbs, one tool, a count, an ending. Driven first by
// a real domain, the harness boundary would get drawn around that domain.

export const TALLY_VERBS = ["TALLY", "HALT"] as const;
export type TallyVerb = (typeof TALLY_VERBS)[number];

export interface TallyPayload {
  verb: TallyVerb;
  count: number;
  note: string;
}

// The workflow's own closed kind set, which the ledger is generic over. The harness
// never imports it, which is what makes the domain-free requirement checkable.
export type TallyKinds = { tally: TallyPayload };

export const TALLY_SCHEMA: Record<string, unknown> = {
  type: "object",
  additionalProperties: false,
  required: ["verb", "note"],
  properties: {
    verb: { type: "string", enum: [...TALLY_VERBS] },
    note: { type: "string", maxLength: 200 },
  },
};

export const SYSTEM = [
  "You keep a running count and nothing else.",
  "Call bump to add to it. When the count has reached its target, emit HALT.",
  "Emit TALLY to keep going. Both verbs require a one-line note.",
].join("\n");
