-- LogLM feature setup: the loglm.view role grant.
--
-- This file once also provisioned a pgvector embedding column/index on
-- findings. Vigil no longer stores per-finding embeddings; the column/index
-- are dropped by 23_drop_finding_embedding.sql.
--
-- Kept as a NEW migration file rather than edits to 01/06 on purpose: the
-- dbInit path only runs a file whose name it hasn't applied before, so edits to
-- already-shipped files never reach existing deployments — a new filename does.
-- Idempotent: safe to re-run.

-- Grant the LogLM page-extension view permission to analyst-and-above roles.
-- Runs on both fresh and existing deployments (see the file header); the WHERE
-- guard makes it idempotent. The extension manifest declares this permission;
-- without it the LogLM tab is hidden outside DEV_MODE. `roles` always exists
-- here — 06_auth_tables.sql runs before this file on every path.
UPDATE roles
SET permissions = permissions || '{"loglm.view": true}'::jsonb,
    updated_at = NOW()
WHERE role_id IN ('role-analyst', 'role-senior-analyst', 'role-manager', 'role-admin')
  AND COALESCE((permissions->>'loglm.view')::boolean, false) = false;
