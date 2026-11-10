-- Three more reasons a sign-in ends (#4172): the person ended it, an admin (or a
-- service account, as containment) did, or everyone was signed out.
--
-- Startup's create_all never changes a check, so the constraint is replaced here.

ALTER TABLE public.sign_ins DROP CONSTRAINT IF EXISTS sign_ins_end_reason_check;
ALTER TABLE public.sign_ins
    ADD CONSTRAINT sign_ins_end_reason_check
    CHECK (end_reason IN ('sign_out', 'expiry', 'revoked', 'idle', 'length_limit',
                          'token_reuse', 'upgrade', 'account_changed',
                          'deactivated', 'erased', 'ended_by_person',
                          'ended_by_admin', 'everyone_signed_out'));
