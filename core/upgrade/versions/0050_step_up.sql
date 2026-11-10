-- Step-up (#4179): the window a confirmation opens counts from confirmed_at, and
-- the third failed confirmation in a row ends the sign-in with a new reason, which
-- is longer than the old column.
-- Startup's create_all may already have built the columns from the model, but it
-- never changes a check, so the constraint is replaced here.

ALTER TABLE public.sign_ins ADD COLUMN IF NOT EXISTS confirmed_at timestamp without time zone;
ALTER TABLE public.sign_ins ADD COLUMN IF NOT EXISTS confirm_failures integer NOT NULL DEFAULT 0;
ALTER TABLE public.sign_ins ALTER COLUMN end_reason TYPE character varying(30);

ALTER TABLE public.sign_ins DROP CONSTRAINT IF EXISTS sign_ins_end_reason_check;
ALTER TABLE public.sign_ins
    ADD CONSTRAINT sign_ins_end_reason_check
    CHECK (end_reason IN ('sign_out', 'expiry', 'revoked', 'idle', 'length_limit',
                          'token_reuse', 'upgrade', 'account_changed',
                          'deactivated', 'erased', 'ended_by_person',
                          'ended_by_admin', 'everyone_signed_out',
                          'sign_in_limit', 'too_many_confirmations'));
