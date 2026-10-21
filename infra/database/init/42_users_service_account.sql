-- Service accounts: machine logins that are not people (D2-17).
-- Today the only one is Medic's gateway, a Viewer created by the enable flow
-- (core/auth/service_account.py). A service account is never locked out after
-- failed logins: a lock would let anyone who learns its name blind Medic. That is
-- safe only because its password is long, random and held by the gateway alone.
-- No API sets this column.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS service_account BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN users.service_account IS
    'Machine login (Medic gateway). Exempt from lockout, Viewer only. Set by core/auth/service_account.py, never by the API.';
