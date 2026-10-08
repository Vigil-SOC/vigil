-- Medic permissions (C6/V3): medic.read to view Medic in the console, medic.admin
-- for every write (feedback, pack import, revert, export). The backend's
-- /api/medic routes (H3) check them; Medic itself can't tell roles apart.
--
-- A new file rather than an edit to 06, for the reason 17_loglm_setup.sql gives:
-- 06 seeds roles with ON CONFLICT DO NOTHING, and Helm's db-init only runs file
-- names it hasn't applied, so only a new file reaches an existing install.
-- scripts/migrate_schema.py (grant_medic_permissions) runs the same statement.
--
-- Admin gets both, Manager medic.read. Viewer, Analyst and Senior Analyst get
-- neither, so Medic's gateway login (a Viewer, 40_users_service_account.sql)
-- can't read Medic back through the backend.
--
-- Existing keys win (grants || permissions), so a grant an operator has turned
-- off stays off: Compose's db-seed re-runs this file on every `up`. A role that
-- already holds every key is left alone. Idempotent.
-- Roles other than the role-* defaults (e.g. the legacy admin/analyst/viewer that
-- migrate_schema.seed_default_roles makes on a create_all-only database) get
-- nothing, as with loglm.view.
UPDATE roles AS r
SET permissions = g.grants || r.permissions,
    updated_at = NOW()
FROM (VALUES
    ('role-admin', '{"medic.read": true, "medic.admin": true}'::jsonb),
    ('role-manager', '{"medic.read": true}'::jsonb)
) AS g (role_id, grants)
WHERE r.role_id = g.role_id
  AND NOT r.permissions ?& ARRAY(SELECT jsonb_object_keys(g.grants));
