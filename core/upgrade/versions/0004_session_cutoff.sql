-- Reject tokens issued before this instant. NULL leaves sessions that already
-- exist valid; a password, email, or role change sets it in the same transaction.

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS tokens_valid_after TIMESTAMP;

COMMENT ON COLUMN public.users.tokens_valid_after IS
    'Reject tokens issued before this instant. NULL until a password, email, or role change ends existing sessions.';
