-- Require single sign-on ends an open non-SSO sign-in with its own reason
-- (#4841). The column is already wide enough; only the check changes. Existing
-- rows keep the reason they have. Startup's create_all never changes a check,
-- so the constraint is replaced here.

ALTER TABLE public.sign_ins DROP CONSTRAINT IF EXISTS sign_ins_end_reason_check;
ALTER TABLE public.sign_ins
    ADD CONSTRAINT sign_ins_end_reason_check
    CHECK (end_reason IN ('sign_out', 'expiry', 'revoked', 'idle', 'length_limit',
                          'token_reuse', 'upgrade', 'account_changed',
                          'deactivated', 'erased', 'ended_by_person',
                          'ended_by_admin', 'everyone_signed_out',
                          'sign_in_limit', 'too_many_confirmations', 'restore',
                          'recovery', 'mfa_reset', 'sso_required'));
