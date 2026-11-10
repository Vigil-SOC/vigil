-- Session policy (#4186). sessions.manage is granted to built-in Admin only,
-- written as a literal so a later catalog edit does not change it. The policy
-- row itself is not seeded: the defaults stand until someone saves one.

UPDATE public.roles
SET permissions = permissions || '{"sessions.manage": true}'::jsonb,
    updated_at = NOW()
WHERE role_id = 'role-admin';

-- A new sign-in past the account's limit ends its oldest: 'sign_in_limit'.
-- Startup's create_all never changes a check, so drop and re-add it.
ALTER TABLE public.sign_ins DROP CONSTRAINT IF EXISTS sign_ins_end_reason_check;
ALTER TABLE public.sign_ins
    ADD CONSTRAINT sign_ins_end_reason_check
    CHECK (end_reason IN ('sign_out', 'expiry', 'revoked', 'idle', 'length_limit',
                          'token_reuse', 'upgrade', 'account_changed',
                          'deactivated', 'erased', 'sign_in_limit'));
