# Medic gateway

The only path between Medic and the Vigil backend, in both directions (C3 §4.6,
A3-2; design and evidence: the SP1 spike). It runs as its own container or pod,
never as a sidecar, and is built from the Python standard library alone.

| Listener | Caller → upstream | Allows |
|---|---|---|
| outbound | Medic → backend | 16 exact `GET`s (SP1 decision 1 a′), plus `GET /_gw/status` (the gateway's login state) |
| inbound | backend → Medic API | X2's 7 reads + `POST /v1/incidents/{id}/feedback`; only `X-Medic-Key`, `X-Medic-Admin`, `X-Request-Id` |

Rules: the raw request-target is checked against a narrow character set and an
exact list; anything ambiguous (`%`, `.`, `//`, `;`, `\`, absolute-form, unknown or
repeated query) is refused, never normalised. The target sent upstream is rebuilt
from the list entry. Upstream headers are built from scratch, redirects are never
followed, and `/api/auth/*` can't be listed. Medic never holds the gateway's
credential.

## Credential

Each request picks the credential from the files at that moment (#4425):

1. **Token.** A readable, non-empty token file: its content goes out as
   `Authorization: Bearer`, with no sign-in or refresh. The file is read on every
   request, so a rotated token is used at once, without a restart. A 401 sets the
   state `unauthorized` and the token is tried again after 300 s, or at once when
   the file's mtime changes. It never stops for good, and a refused token never
   falls back to the password.
2. **Password.** Otherwise, a readable, non-empty password file plus
   `VIEWER_USER`: the gateway logs in as a Viewer itself (fixed User-Agent,
   refresh before expiry and once on a 401, stop after 2 bad passwords until the
   file changes). An empty password file counts as absent. This path is kept for
   now and removed by a later issue.
3. **Neither:** state `no_credential`.

`GET /_gw/status` returns `{"state", "since", "credential"}`, with `credential`
one of `token`, `password` or `none`. The states are `init`, `ok`,
`unauthorized` and `no_credential`, plus the password path's `bad_password`,
`bad_password_stopped`, `locked`, `rate_limited`, `rejected_after_login`,
`backend_unreachable` and `login_error` (`no_password` is no longer set; Medic
still accepts it). Medic's `gateway_session` sensor reads the state and ignores
`credential`.

## Settings

| Variable | Example |
|---|---|
| `VIGIL_MEDIC_GATEWAY_BACKEND` | `backend:6987` |
| `VIGIL_MEDIC_GATEWAY_MEDIC` | `medic:8470` |
| `VIGIL_MEDIC_GATEWAY_OUT_BIND` | `medic-gateway-out:8471` (an alias on `medic-private`) |
| `VIGIL_MEDIC_GATEWAY_IN_BIND` | `medic-gateway-in:8470` (an alias on `deeptempo-network`) |
| `VIGIL_MEDIC_GATEWAY_TOKEN_FILE` | default `/run/secrets/medic_gateway_token` |
| `VIGIL_MEDIC_GATEWAY_VIEWER_USER` | the service account's username; needed only for the password path |
| `VIGIL_MEDIC_GATEWAY_VIEWER_PASSWORD_FILE` | default `/run/secrets/medic_viewer_password` |
| `VIGIL_CONTEXT_PATH` | Vigil's own; the gateway adds it to every upstream path |

Each bind name is resolved at start-up and the listener binds to that address
only. It refuses to start (exit 2) if `AGENT_INTERNAL_TOKEN`, `AGENT_INTERNAL_TOKEN_PREVIOUS`, `VIGIL_TOOLS_TOKEN`,
`DAEMON_WEBHOOK_TOKEN`, `VIGIL_MEDIC_API_KEY`, a Viewer password or
`VIGIL_MEDIC_GATEWAY_TOKEN` is in its environment. It starts without
`VIEWER_USER`; the password path then has no credential to use.

```
python -m services.medic_gateway run
python -m services.medic_gateway check   # exit 0 if both listeners answer
```

## Checks

Its own lock (no runtime dependencies; dev only). From the repo root, with the
venv outside `services/`:

```
UV_PROJECT_ENVIRONMENT=../../.venv-medic-gateway uv run --project services/medic_gateway ruff check services/medic_gateway
UV_PROJECT_ENVIRONMENT=../../.venv-medic-gateway uv run --project services/medic_gateway ruff format --check services/medic_gateway
UV_PROJECT_ENVIRONMENT=../../.venv-medic-gateway uv run --project services/medic_gateway lint-imports
```

## Image

`infra/docker/Dockerfile.medic-gateway`: uid/gid 10002, read-only root
filesystem, `HEALTHCHECK` running `check`.

```
docker build -f infra/docker/Dockerfile.medic-gateway -t vigil-medic-gateway .
```
