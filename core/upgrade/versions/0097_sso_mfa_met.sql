-- How an SSO sign-in met MFA, and an end sooner than the policy's when that MFA
-- came from an earlier sign-in at the provider (#4757). Older rows stay null:
-- "not recorded (before this check)". core/auth/sign_ins.py reads both.

ALTER TABLE public.sign_ins
    ADD COLUMN IF NOT EXISTS mfa_met VARCHAR(20),
    ADD COLUMN IF NOT EXISTS ends_at TIMESTAMP WITHOUT TIME ZONE;

ALTER TABLE public.sign_ins DROP CONSTRAINT IF EXISTS sign_ins_mfa_met_check;
ALTER TABLE public.sign_ins ADD CONSTRAINT sign_ins_mfa_met_check
    CHECK (mfa_met IN ('provider_mfa', 'provider_phr', 'vigil_factor', 'step_down', 'none'));
