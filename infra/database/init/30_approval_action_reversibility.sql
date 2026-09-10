-- Reversibility and idempotency on approval_actions (#827).
--
-- Confidence already gates auto-approval in create_action. That cannot say
-- whether the work can be undone, so an irreversible action still auto-approved
-- at 0.95. Nor can it say the work has already run: isolating the same host
-- twice was two rows and two executions.
--
-- reversibility defaults to reversible so today's isolate/block auto-approve
-- path is unchanged; callers that mean irreversible pass it. idempotency_key
-- is unique among non-failed rows (NULLs do not collide); a failed row may
-- be retried with the same key.

ALTER TABLE approval_actions
    ADD COLUMN IF NOT EXISTS reversibility VARCHAR(16) NOT NULL DEFAULT 'reversible';

ALTER TABLE approval_actions
    DROP CONSTRAINT IF EXISTS approval_actions_reversibility_check;
ALTER TABLE approval_actions
    ADD CONSTRAINT approval_actions_reversibility_check
        CHECK (reversibility IN ('reversible', 'irreversible'));

ALTER TABLE approval_actions
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_actions_idempotency_key
    ON approval_actions (idempotency_key)
    WHERE idempotency_key IS NOT NULL AND status <> 'failed';

COMMENT ON COLUMN approval_actions.reversibility IS
    'Whether the action can be undone. Irreversible rows always require approval, regardless of confidence.';
COMMENT ON COLUMN approval_actions.idempotency_key IS
    'Caller-supplied key; unique among non-failed rows so a repeated isolate of the same target is one row.';
