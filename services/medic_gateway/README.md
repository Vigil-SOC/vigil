# Medic gateway

The only path between Medic and the Vigil backend, in both directions (C3 §4.6,
A3-2; design and evidence: the SP1 spike). It runs as its own container or pod,
never as a sidecar, and is built from the Python standard library alone.

| Listener | Caller → upstream | Allows |
|---|---|---|
| outbound | Medic → backend | 15 exact `GET`s (SP1 decision 1 a′), plus `GET /_gw/status` (the gateway's login state) |
| inbound | backend → Medic API | X2's 8 reads + `POST /v1/incidents/{id}/feedback`; only `X-Medic-Key`, `X-Medic-Admin`, `X-Request-Id` |

Rules: the raw request-target is checked against a narrow character set and an
exact list; anything ambiguous (`%`, `.`, `//`, `;`, `\`, absolute-form, unknown or
repeated query) is refused, never normalised. The target sent upstream is rebuilt
from the list entry. Upstream headers are built from scratch, redirects are never
followed, and `/api/auth/*` can't be listed. The gateway logs in as a Viewer
itself (fixed User-Agent, password from a file, refresh before expiry and once
on a 401, stop after 2 bad passwords until the file changes). Medic never holds
that credential.

## Settings

| Variable | Example |
|---|---|
| `VIGIL_MEDIC_GATEWAY_BACKEND` | `backend:6987` |
| `VIGIL_MEDIC_GATEWAY_MEDIC` | `medic:8470` |
| `VIGIL_MEDIC_GATEWAY_OUT_BIND` | `medic-gateway:8471` (an alias on `medic-net`) |
| `VIGIL_MEDIC_GATEWAY_IN_BIND` | `gateway-in:8470` (an alias on `deeptempo-network`) |
| `VIGIL_MEDIC_GATEWAY_VIEWER_USER` | the service account's username |
| `VIGIL_MEDIC_GATEWAY_VIEWER_PASSWORD_FILE` | default `/run/secrets/medic_viewer_password` |
| `VIGIL_CONTEXT_PATH` | Vigil's own; the gateway adds it to every upstream path |

Each bind name is resolved at start-up and the listener binds to that address
only. It refuses to start (exit 2) if `AGENT_INTERNAL_TOKEN`, `VIGIL_TOOLS_TOKEN`,
`DAEMON_WEBHOOK_TOKEN`, `VIGIL_MEDIC_API_KEY` or a Viewer password is in its
environment.

```
python -m services.medic_gateway run
python -m services.medic_gateway check   # exit 0 if both listeners answer
```

## Tests

Its own lock (no runtime dependencies; dev only). From the repo root, with the
venv outside `services/` (the repo's ratchets scan every `.py` under it):

```
UV_PROJECT_ENVIRONMENT=../../.venv-medic-gateway uv run --project services/medic_gateway pytest services/medic_gateway
UV_PROJECT_ENVIRONMENT=../../.venv-medic-gateway uv run --project services/medic_gateway ruff check services/medic_gateway
UV_PROJECT_ENVIRONMENT=../../.venv-medic-gateway uv run --project services/medic_gateway ruff format --check services/medic_gateway
UV_PROJECT_ENVIRONMENT=../../.venv-medic-gateway uv run --project services/medic_gateway lint-imports
```

`tests/cases.py` is the bypass table (C3 check 10); `tests/test_inbound.py` pins
the inbound list to `services/medic/contracts/medic-api.openapi.yaml`.

## Image

`infra/docker/Dockerfile.medic-gateway`: uid/gid 10002, read-only root
filesystem, `HEALTHCHECK` running `check`.

```
docker build -f infra/docker/Dockerfile.medic-gateway -t vigil-medic-gateway .
```
