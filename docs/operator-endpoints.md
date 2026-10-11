# Operator endpoints

A few API routes exist for operators and have no screen in the console. Log lines and error messages point you at some of them. This page says what each one does, who may call it, and what it changes.

| Route | Permission | Changes |
|---|---|---|
| `GET /api/config/secrets/status` | any signed-in user | nothing |
| `POST /api/config/secrets/reinit` | `settings.write` | the secrets manager in one API process |
| `POST /api/config/secrets/migrate-to-encrypted` | `settings.write` | `secrets.enc` and the State Directory `.env` |
| `POST /api/storage/init-schema` | `settings.write` | creates tables in the connected database |

The seeded **Manager** and **Admin** roles hold `settings.write`. Under `DEV_MODE=true` every request runs as an admin with every permission.

None of these routes writes an audit record.

## Calling them with curl

The API accepts a session cookie or an `Authorization: Bearer` token. From a script, use the token.

Every `POST` must also pass the CSRF check, unless `VIGIL_CSRF_ENABLED=false` or `VIGIL_CSRF_REPORT_ONLY=true`. The check only compares the `X-CSRF-Token` header with the `csrf_token` cookie, so a script can pick any value and send it as both. This applies to the login call too.

The token is bound to the `User-Agent` it was issued to. Send the same one on every call. A token copied from a browser will not work from curl.

```bash
API=http://localhost:6987          # add VIGIL_CONTEXT_PATH here if you set one
UA="vigil-ops"
CSRF=$(openssl rand -hex 16)
read -rsp "Password: " PW; echo

TOKEN=$(curl -s -A "$UA" "$API/api/auth/login" \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" -b "csrf_token=$CSRF" \
  -d "{\"username_or_email\": \"admin\", \"password\": \"$PW\"}" \
  | jq -r .access_token)
# With MFA on, add "mfa_code": "123456" to the login body.

curl -s -A "$UA" -X POST "$API/api/config/secrets/reinit" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-CSRF-Token: $CSRF" -b "csrf_token=$CSRF" \
  -H "Content-Type: application/json" \
  -d '{"write_backend": "encrypted"}'
```

The `GET` route needs only the `Authorization` header and the same `-A "$UA"`.

## One process at a time

The secrets manager is held in memory by each API process. The Docker backend image (`infra/docker/Dockerfile.backend`) starts uvicorn with `--workers 2`, so there are two. `secrets/status` and `secrets/reinit` reach whichever worker answers the request. The daemon and the ARQ worker are separate processes and keep their own secrets manager.

Nothing these routes do to the secrets manager is saved. On restart every process picks its write backend from `SECRETS_BACKEND` again (default `encrypted`). If you need the change everywhere, set `SECRETS_BACKEND` and restart.

## `GET /api/config/secrets/status`

Reports which secrets backend this process writes to, which ones it reads from, and where they live on disk. It reads the state and changes nothing.

Use it when a saved credential does not seem to land in `secrets.enc`, or after the startup log says `write_backend=... but expected=...`.

No request body. The response:

```json
{
  "encrypted": {
    "available": true,
    "path": "/home/vigil/.vigil/secrets.enc",
    "secrets_file_exists": true,
    "master_key_present": true,
    "master_key_path": "/home/vigil/.vigil/master.key",
    "description": "..."
  },
  "environment": { "available": true, "description": "..." },
  "dotenv": { "available": true, "path": "/home/vigil/.vigil/.env", "exists": false, "description": "..." },
  "keyring": { "available": false, "description": "..." },
  "cryptography_available": true,
  "write_backend": "encrypted",
  "read_backends": ["EncryptedFileBackend", "EnvironmentBackend", "DotEnvBackend"],
  "expected_write_backend": "encrypted"
}
```

`expected_write_backend` is `SECRETS_BACKEND` as this process sees it now. When it differs from `write_backend`, the process is not writing where you configured it to. The response holds file paths, not secret values.

## `POST /api/config/secrets/reinit`

Throws away this process's secrets manager and builds a new one. The new one re-reads `SECRETS_BACKEND`, `ENABLE_KEYRING` and `general_config.json`, and reloads `secrets.enc` and the State Directory `.env` from disk.

Use it when `secrets/status` shows the wrong write backend, for example after you changed `SECRETS_BACKEND` in `.env` without a restart. The running process still has the old value, so pass the backend you want in the body.

Request body, optional:

```json
{ "write_backend": "encrypted" }
```

`write_backend` is one of `encrypted`, `env`, `dotenv` or `keyring`. Leave it out to use `SECRETS_BACKEND` from the process environment, which is the value the process started with unless something changed it.

The response is `{"reloaded": true, "status": {...}}`, where `status` has the same shape as `secrets/status`.

What it does not do:

- It does not delete, re-encrypt or move any stored secret. Files on disk are left as they are.
- It cannot fix a missing `cryptography` package. The process checks for it once, at import. If it was missing then, `encrypted` still falls back to `dotenv` after a reinit. Install it and restart.
- It reaches one API worker. See [One process at a time](#one-process-at-a-time).

Things to watch:

- `env` writes to the process environment only. Secrets saved that way are gone on restart.
- `keyring` also turns on keyring reads. On macOS that can raise keychain prompts.
- A value outside the four names is not rejected. Writes go to the encrypted store, but `write_backend` in the response shows the name you sent.
- If you force a backend different from `SECRETS_BACKEND`, `secrets/status` will keep showing the two as different.

## `POST /api/config/secrets/migrate-to-encrypted`

Copies secrets from the State Directory `.env` (`$VIGIL_DIR/.env`, `~/.vigil/.env` by default, shown as `dotenv.path` in `secrets/status`) into `secrets.enc`, then removes them from the `.env`.

It does not read or change the repo-root `.env`. The secrets manager only ever reads that file.

Use it to move a deployment off the legacy dotenv store.

Request body, optional:

```json
{ "keys": ["SOME_API_KEY"], "remove_from_dotenv": true }
```

- `keys` limits the move to those names. Leave it out to move every key in the file, not only names Vigil knows to be secrets.
- `remove_from_dotenv` defaults to `true`. With `false`, the values are still written to `secrets.enc`; only the `.env` is left alone. It is not a dry run.

For each key with a non-empty value:

- Not in `secrets.enc`: copied in, then removed from the `.env`.
- In `secrets.enc` with the same value: removed from the `.env`.
- In `secrets.enc` with a different value: both are left alone and the key is reported as a conflict. The encrypted value is the one Vigil reads, because the encrypted store is read first.

The response:

```json
{
  "migrated": ["SOME_API_KEY"],
  "already_present": [],
  "conflicts": [{ "key": "OTHER_KEY", "reason": "value_mismatch" }],
  "errors": [],
  "encrypted_available": true,
  "dotenv_path": "/home/vigil/.vigil/.env"
}
```

It returns 200 even when nothing moved. Read `errors` and `conflicts`.

Side effects and limits:

- Removing a key rewrites the `.env` from the keys it parsed. Comments and blank lines in that file are lost.
- If `master.key` does not exist and neither does `secrets.enc`, the first write creates a new `master.key`. Back it up. Without it `secrets.enc` cannot be read.
- If `secrets.enc` exists but cannot be decrypted (wrong or missing `master.key`), Vigil refuses to write to it. Each key is reported under `errors` and the `.env` is not changed.
- If the `cryptography` package is missing, nothing is moved and `errors` holds one entry for `<all>`.
- With `VIGIL_DISABLE_DOTENV` set, the State Directory `.env` is not read, so there is nothing to move.

## `POST /api/storage/init-schema`

Creates Vigil's tables in the database the API is connected to, when that database has none of them yet. It is the step after `POST /api/storage/reconnect` reports `requires_schema_init: true`.

No request body. It first compares the database with the ORM models:

| State | Meaning | Result |
|---|---|---|
| `empty` | none of Vigil's tables exist | creates the tables, `200 {"success": true, "message": "Schema created", ...}` |
| `ok` | every table and column is present | changes nothing, `200 {"success": true, "message": "Schema already up to date", ...}` |
| `drifted` | some tables or columns are missing, or a column is `NOT NULL` where the model allows null | `409`; run `scripts/migrate_schema.py` instead |
| `unknown` | the schema could not be inspected | `409` |

A `200` response also carries the schema report: `state`, `missing_tables`, `missing_columns` and `not_null_columns`.

Things to know:

- `empty` means no table with a Vigil name. Tables with other names do not count. A database that holds only another application's tables is `empty`, and Vigil's tables are created next to them. Check the target before you call this.
- It creates tables only. It does not seed roles or other reference data. Run `python scripts/seed_reference_data.py` afterwards.
- Your login and permission are checked against the same database. If it has no Vigil tables, it has no `users` table either, and the request can fail before the handler runs. For a brand-new database, `python scripts/init_schema.py` runs the same create step from a shell without a login.
- The `drifted` error message lists missing columns only. Call `POST /api/storage/reconnect` to see `missing_tables` too.
