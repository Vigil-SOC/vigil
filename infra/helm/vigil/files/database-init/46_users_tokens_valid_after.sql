-- Session cutoff. A token issued before this instant is rejected. NULL leaves
-- sessions that already exist valid; a password, email, or role change sets it.
-- Revision 0004_session_cutoff.sql is the same change for a database that
-- upgrades from 0001_baseline, which was snapshotted before this file.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS tokens_valid_after TIMESTAMP;

COMMENT ON COLUMN users.tokens_valid_after IS
    'Reject tokens issued before this instant. NULL until a password, email, or role change ends existing sessions.';
