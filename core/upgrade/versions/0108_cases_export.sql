-- cases.export sends a whole case outside Vigil in one signed file. Built-in
-- Admin and Manager, written as literals so a later catalog edit does not
-- change them.

UPDATE public.roles
SET permissions = permissions || '{"cases.export": true}'::jsonb,
    updated_at = NOW()
WHERE role_id IN ('role-admin', 'role-manager');
