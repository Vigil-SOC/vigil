-- Sign-ins the server enforces (#4169). The row now records the last own action,
-- how and from where the person signed in, and the refresh generation; the
-- tokens' own copies of the browser binding and revocation state go.
--
-- Startup's create_all may already have built sign_ins from the model with these
-- columns, and it never changes a check, so every step tolerates either state.

ALTER TABLE public.sign_ins
    ADD COLUMN IF NOT EXISTS last_action_at timestamp without time zone DEFAULT now() NOT NULL,
    ADD COLUMN IF NOT EXISTS auth_method character varying(20) DEFAULT 'password' NOT NULL,
    ADD COLUMN IF NOT EXISTS user_agent text,
    ADD COLUMN IF NOT EXISTS device character varying(20),
    ADD COLUMN IF NOT EXISTS address text,
    ADD COLUMN IF NOT EXISTS refresh_generation integer DEFAULT 0 NOT NULL,
    ADD COLUMN IF NOT EXISTS refresh_rotated_at timestamp without time zone;

-- 'revoked' stays valid for the rows that already carry it.
ALTER TABLE public.sign_ins DROP CONSTRAINT IF EXISTS sign_ins_end_reason_check;
ALTER TABLE public.sign_ins
    ADD CONSTRAINT sign_ins_end_reason_check
    CHECK (end_reason IN ('sign_out', 'expiry', 'revoked', 'idle', 'length_limit',
                          'token_reuse', 'upgrade', 'account_changed',
                          'deactivated', 'erased'));

-- Open sign-ins hold tokens minted without a refresh generation or the stored
-- browser; they sign in again.
UPDATE public.sign_ins
SET ended_at = now(), end_reason = 'upgrade'
WHERE ended_at IS NULL;
