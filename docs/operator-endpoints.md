# Operator endpoints

A few API routes exist for operators and have no screen in the console. Log lines and API responses point you at some of them. This page says what each one does, who may call it, and what it changes.

| Route | Permission | Changes |
|---|---|---|
| `GET /api/config/secrets/status` | any signed-in user | nothing |
| `POST /api/config/secrets/migrate-to-encrypted` | `platform.write` | `secrets.enc` and the State Directory `.env` |

The seeded **Manager** and **Admin** roles hold `platform.write`. Under `DEV_MODE=true` every request runs as an admin with every permission.

Only `migrate-to-encrypted` writes an audit record (config type `secrets`, key `SECRETS_MIGRATE_TO_ENCRYPTED`, naming the keys it touched and never their values). `secrets/status` writes none.

## Calling them with curl

From a script, use a personal API token. Mint one in the console: **Settings**, **Team & access**, the **API tokens** card. Give it a label and a lifetime, then **Mint**. The token is shown once, so store it then.

- A person's token lasts 90 days by default and 365 at most. A service account's lasts 365 days.
- A Manager or Admin must confirm it's them to mint one, so they need two-step sign-in set up.
- A token can do what its holder's role allows. It can never mint or rotate a token, or change an account.
- For unattended work, use a service account's token. Someone who holds `users.write` mints it, from the **Tokens of** card on that account.

A token needs no login call, no `User-Agent` match, and no CSRF header or cookie, and it is not counted as a sign-in. It is never asked to confirm it's you, so `migrate-to-encrypted` works with it at any time. It is recorded against its holder.

```bash
API=http://localhost:6987          # add VIGIL_CONTEXT_PATH here if you set one
read -rsp "Token: " VIGIL_TOKEN; echo   # a vgl_api_... token; or export it in the shell

curl -s "$API/api/config/secrets/status" \
  -H "Authorization: Bearer $VIGIL_TOKEN"

curl -s -X POST "$API/api/config/secrets/migrate-to-encrypted" \
  -H "Authorization: Bearer $VIGIL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["SOME_API_KEY"]}'
```

### Why not log in with a password

A script can still sign in with a password and use the bearer token the login returns, but that is a person's sign-in and it follows the person's rules:

- It ends at the idle limit and at the length limit.
- Where an admin has set a sign-in limit, it counts toward that limit and a newer sign-in can end it. The limit is off by default.
- It is bound to the `User-Agent` it signed in with. Send the same one on every call.
- Every `POST` must pass the CSRF check, unless `VIGIL_CSRF_ENABLED=false` or `VIGIL_CSRF_REPORT_ONLY=true`. The check compares the `X-CSRF-Token` header with the `csrf_token` cookie, so a script can pick any value and send it as both. This applies to the login call too.
- With two-step sign-in on, the login needs a current `mfa_code`.
- A privileged write such as `migrate-to-encrypted` asks it to confirm it's you. With no two-step sign-in it is refused outright. With it, the call works only inside the window the login's code opened; after that it is refused until the person confirms again.

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
