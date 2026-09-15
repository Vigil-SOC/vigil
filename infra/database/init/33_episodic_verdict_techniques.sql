-- Which ATT&CK techniques a Verdict's evidence cited (#898).
--
-- The hunt harness already computes this (`citedTechniques`) and the Distil
-- dropped it, so a coverage check could not ask "have we hunted T1071.001?".
-- Persisted as a text[] in the shape of `subject_entities`: empty means
-- known-to-be-none, never unknown. This is a column on the Verdict and not a
-- Technique entity -- `ENTITY_KEY_TYPES` does not grow and recall is unchanged.
--
-- Added here rather than in 26_episodic_memory.sql, which the init job treats
-- as already applied. Existing rows default to empty; the Distil's version bump
-- re-derives every hunt, and a Case-authored Verdict cites none.
ALTER TABLE episodic_verdicts
    ADD COLUMN IF NOT EXISTS techniques text[] NOT NULL DEFAULT ARRAY[]::text[];

CREATE INDEX IF NOT EXISTS idx_episodic_verdicts_techniques
    ON episodic_verdicts USING GIN (techniques);

COMMENT ON COLUMN episodic_verdicts.techniques IS
    'Distinct ATT&CK ids the gathered evidence bearing on this Hypothesis cited; empty is known-to-be-none.';
