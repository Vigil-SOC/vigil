import type { Memory } from "./seams.js";

// Notes from somewhere the caller named, not a search over everything: what a
// finished run carries forward is its own workflow's to say.
export type Notes = (limit: number) => Promise<readonly string[]>;

// The only implementation shipped. Recall returning nothing is the seam's default,
// not a stub: a run never silently depends on what a backend happened to remember.
//
// The keyed read is the exception, and throws. Empty lists mean known-to-be-none,
// so answering them here would say those entities have no history when the truth
// is that nothing was asked -- see RecallUnavailable. The caller journals the throw
// as a read that did not happen, which is what it was.
export const nullMemory: Memory = {
  recall: async () => [],
  entities: async () => {
    throw new Error("no episodic memory is wired behind this seam");
  },
  remember: async () => {},
};

// The cue is ignored because the caller already answered what to recall from.
// Remembering is still nothing: recall reads a run that is over.
//
// The keyed read is a different tier and this tier does not serve it, so it throws
// for the reason nullMemory does: a parent run's notes are not an entity's history,
// and answering known-to-be-none here would say memory was asked when it was not.
export function recalling(notes: Notes): Memory {
  return {
    recall: async (_cue, limit) => notes(limit),
    entities: nullMemory.entities,
    remember: async () => {},
  };
}
