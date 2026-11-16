-- skills.approve releases a held operator skill to agents. Built-in Admin only,
-- written as a literal so a later catalog edit does not change it.

UPDATE public.roles
SET permissions = permissions || '{"skills.approve": true}'::jsonb,
    updated_at = NOW()
WHERE role_id = 'role-admin';
