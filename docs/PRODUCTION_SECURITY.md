# Production Security Checklist

This document enumerates every security-relevant configuration switch and
provides the recommended production values. Each item has a single env var
or file location so it can be audited mechanically.

---

## Critical — Must Set Before Production

| Switch | Env Variable | Dev Default | Production Value | Notes |
|--------|-------------|-------------|------------------|-------|
| Dev Mode | `DEV_MODE` | `true` | `false` | Bypasses ALL authentication. Never enable in prod. |
| JWT Secret | `JWT_SECRET_KEY` | (generated dev key) | 64+ char random string | `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| CSRF Enforcement | `VIGIL_CSRF_REPORT_ONLY` | `false` | `false` | Must be `false` to reject forged requests. |
| Token Revocation | `REVOCATION_FAIL_OPEN` | `false` | `false` | Reject tokens when Redis is down rather than allowing through. |
| Access Token TTL | `JWT_ACCESS_EXPIRATION_MINUTES` | `30` | `15`–`30` | Shorter = less window after compromise. |
| Refresh Token TTL | `JWT_REFRESH_EXPIRATION_DAYS` | `7` | `7` | Max session length before forced re-login. |
| Cookie Secure Flag | `VIGIL_COOKIE_SECURE` | `false` | `true` | Cookies only sent over HTTPS. |
| Cookie SameSite | `VIGIL_COOKIE_SAMESITE` | `lax` | `strict` | Prevents cross-origin cookie send. |
| PostgreSQL Password | `docker-compose.yml` | `vigil` | Strong random | Change default docker-compose password. |
| LLM Budget Unlimited | `LLM_BUDGET_UNLIMITED` | `false` | `false` | Keep cost guardrails active. |

---

## Authentication & Session Security

### JWT Tokens

- **Access tokens** expire after `JWT_ACCESS_EXPIRATION_MINUTES` (default 30 min).
- **Refresh tokens** expire after `JWT_REFRESH_EXPIRATION_DAYS` (default 7 days).
- Tokens include a **session fingerprint** (`sfp` claim) binding the token
  to the client's IP + User-Agent. A stolen token replayed from a different
  client will fail fingerprint validation in the auth middleware.
- Token generation: `AuthService.generate_jwt_token(user, token_type, request_ip, user_agent)`
- Token fingerprint check: `AuthService.verify_session_fingerprint(payload, request_ip, user_agent)`

### Token Revocation (Redis)

- **Fail-closed by default** (`REVOCATION_FAIL_OPEN=false`): if Redis is
  unreachable during a token verification, the request is rejected.
- Set `REVOCATION_FAIL_OPEN=true` only if you prefer availability over
  security during Redis outages.
- Revocation happens on: logout (per-JTI blacklist), password change
  (per-user cutoff), role change (per-user cutoff).

### MFA (TOTP)

- MFA secrets are **encrypted at rest** using Fernet symmetric encryption
  derived from `JWT_SECRET_KEY`.
- **Recovery codes**: 10 codes generated when MFA is enabled (i.e. after the
  first TOTP code is confirmed), not at setup. Each is bcrypt-hashed and stored;
  the plaintext is returned exactly once and cannot be retrieved again.
  Single-use: consumed on verification and removed from the list.
- Legacy unencrypted secrets are handled gracefully (transparent fallback).
- Endpoint: `POST /api/auth/mfa/setup` → returns TOTP secret + QR URI.
- Endpoint: `POST /api/auth/mfa/verify` → confirms the first TOTP code, enables
  MFA, and returns the one-time recovery codes.
- Endpoint: `POST /api/auth/mfa/recovery-codes` → regenerates a fresh set of
  recovery codes, invalidating the previous set.

### Account Lockout

- After `AUTH_LOCKOUT_THRESHOLD` (default 5) consecutive failed logins, the
  account locks for `AUTH_LOCKOUT_DURATION_MINUTES` (default 15 min).
- Lockout is time-based; successful auth after expiry resets the counter.

### Password Policy

- Minimum length: `AUTH_MIN_PASSWORD_LENGTH` (default 12, NIST 800-63B aligned).
- Password history: last `AUTH_PASSWORD_HISTORY_LIMIT` (default 5) are rejected.
- bcrypt with random salt for all hashes.

---

## CSRF Protection

- Enabled via `VIGIL_CSRF_ENABLED=true` (default).
- Double-submit cookie pattern: frontend reads `csrf_token` cookie and echoes
  it as `X-CSRF-Token` header on state-changing requests.
- **Enforcement**: `VIGIL_CSRF_REPORT_ONLY=false` rejects violations. Set to
  `true` only during initial deployment monitoring.
- Exempt paths (webhooks, ingest): `VIGIL_CSRF_EXEMPT_PATHS=/api/webhooks/,/api/ingest/`

---

## Cookie Configuration

| Variable | Default | Production |
|----------|---------|------------|
| `VIGIL_COOKIE_SECURE` | `false` | `true` (HTTPS only) |
| `VIGIL_COOKIE_SAMESITE` | `lax` | `strict` |
| `VIGIL_COOKIE_DOMAIN` | (none) | Your domain |
| `VIGIL_COOKIE_PATH` | `/` | `/` |

Tokens are stored in **HttpOnly** cookies (not accessible to JavaScript).
`Secure=true` requires HTTPS — the browser will not send the cookie over
plain HTTP.

---

## Security Headers

Set via the security headers middleware. Defaults are conservative:

- `Strict-Transport-Security: max-age=31536000; includeSubDomains` (HSTS)
- `Content-Security-Policy`: configurable via `VIGIL_CSP_POLICY`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`

---

## Rate Limiting

- Applied globally via middleware (`backend/middleware/rate_limit.py`).
- Auth endpoints have tighter limits to prevent brute force.
- Configure with `slowapi` settings in the middleware.

---

## Deployment Hardening Checklist

```bash
# 1. Generate secrets
export JWT_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(64))")

# 2. Disable dev mode
export DEV_MODE=false

# 3. Enforce CSRF
export VIGIL_CSRF_REPORT_ONLY=false

# 4. Secure cookies (requires HTTPS)
export VIGIL_COOKIE_SECURE=true
export VIGIL_COOKIE_SAMESITE=strict

# 5. Fail-closed revocation
export REVOCATION_FAIL_OPEN=false

# 6. Set token lifetimes
export JWT_ACCESS_EXPIRATION_MINUTES=15
export JWT_REFRESH_EXPIRATION_DAYS=7

# 7. Ensure Redis is running (required for revocation)
# Redis must be reachable at REDIS_URL for token blacklist to work.

# 8. Change default DB password
# Edit docker-compose.yml or use POSTGRES_PASSWORD env var.
```

---

## Migration Notes

### Existing MFA Secrets

Pre-existing `mfa_secret` values stored as plaintext will continue to work.
The decrypt function falls back to raw value if Fernet decryption fails.
New secrets are always stored encrypted. To force re-encryption of all
existing secrets, run a one-time migration script:

```python
from backend.services.auth_service import AuthService
from database.connection import get_db_session
from database.models import User

session = get_db_session()
for user in session.query(User).filter(User.mfa_secret.isnot(None)).all():
    # Check if already encrypted (Fernet tokens start with 'gAAAAA')
    if not user.mfa_secret.startswith("gAAAAA"):
        user.mfa_secret = AuthService._encrypt_mfa_secret(user.mfa_secret)
session.commit()
session.close()
```

### New Database Column

The `mfa_recovery_codes` column (JSONB, default `[]`) must be added to
existing deployments. For fresh installs it's in `06_auth_tables.sql`.
For existing databases:

```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_recovery_codes JSONB NOT NULL DEFAULT '[]';
```

### Session Fingerprint Backwards Compatibility

Tokens issued before the fingerprint feature won't have the `sfp` claim.
`verify_session_fingerprint()` returns `True` for tokens without `sfp`,
so existing sessions won't break. New tokens always include it.
