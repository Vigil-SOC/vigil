# Operator endpoints

A few API routes exist for operators and have no screen in the console. Log lines and API responses point you at some of them. This page says what each one does, who may call it, and what it changes.

| Route | Permission | Changes |
|---|---|---|
| `GET /api/config/secrets/status` | any signed-in user | nothing |
| `POST /api/config/secrets/migrate-to-encrypted` | `settings.write` | `secrets.enc` and the State Directory `.env` |
| `POST /api/storage/init-schema` | `settings.write` | creates tables in the connected database |

The seeded **Manager** and **Admin** roles hold `settings.write`. Under `DEV_MODE=true` every request runs as an admin with every permission.

Only `migrate-to-encrypted` writes an audit record (config type `secrets`, key `SECRETS_MIGRATE_TO_ENCRYPTED`, naming the keys it touched and never their values). The other two write none.

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

curl -s -A "$UA" -X POST "$API/api/config/secrets/migrate-to-encrypted" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-CSRF-Token: $CSRF" -b "csrf_token=$CSRF" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["SOME_API_KEY"]}'
```

The `GET` route needs only the `Authorization` header and the same `-A "$UA"`.

## `GET /api/config/secrets/status`

Reports which secrets backend this process writes to, which ones it reads from, and where they live on disk. It reads the state and changes nothing.

Use it when a saved credential does not seem to land in `secrets.enc`, or after the startup log says `write_backend=... but expected=...`.

The answer describes the API process that took the request. The Docker backend image (`infra/docker/Dockerfile.backend`) starts uvicorn with `--workers 2`, and the daemon and the ARQ worker are processes of their own; each picks its write backend once, at startup, from `SECRETS_BACKEND` (default `encrypted`), the `cryptography` package and the master key. Nothing changes that choice while a process runs. If the status shows the wrong backend, fix the cause and restart the API, daemon and worker.

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

`expected_write_backend` is `SECRETS_BACKEND` as this process sees it now. When it differs from `write_backend`, the process is not writing where you configured it to: `encrypted` falls back to `dotenv` when `cryptography` is not installed. When a repo-root `.env` exists, `read_backends` lists `DotEnvBackend` twice: that file, read-only, and then the State Directory one. The response holds file paths, not secret values.

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

The status code follows the report, and the report is the body in every case:

- `200` when every key was migrated, already present or in conflict, and also when the `.env` held nothing to move. Read `conflicts`.
- `503` when the encrypted backend is unavailable: `encrypted_available` is `false`, nothing is moved, and `errors` holds one entry for `<all>`. That means the `cryptography` package is missing; install it and restart.
- `500` when any key errored. An error on one key does not stop the others, so `migrated` still names the keys that did move.

Side effects and limits:

- Removing a key rewrites the `.env` from the keys it parsed. Comments and blank lines in that file are lost.
- If `master.key` does not exist and neither does `secrets.enc`, the first write creates a new `master.key`. Back it up. Without it `secrets.enc` cannot be read.
- If `secrets.enc` exists but cannot be decrypted (wrong or missing `master.key`), Vigil refuses to write to it. Each key is reported under `errors`, the response is `500`, and the `.env` is not changed.
- With `VIGIL_DISABLE_DOTENV` set, the State Directory `.env` is not read, so there is nothing to move.
- Every call writes one audit record naming the keys in `migrated`, `already_present`, `conflicts` and `errors`, and the `remove_from_dotenv` value. Secret values are never recorded.

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
