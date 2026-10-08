# Medic Docker proxy

Medic's only path to the Docker Engine API on Compose (C3 §4.3; design and
evidence: the SP2 spike). It alone mounts the Docker socket; Medic gets its URL,
never the socket. Built from the Python standard library alone (asyncio).

| Route (any `/v1.NN` prefix) | Query allowed | Reply |
|---|---|---|
| `GET /_ping` | none | piped |
| `GET /containers/json` | `all`, `limit`, `filters` (JSON object) | **projected** to `Id, Names, Image, Labels, State, Status, Created` |
| `GET /containers/{id}/json` | none | **projected** to `Id, Name, Created, RestartCount, Image`, `State{…, Health{Status, FailingStreak}}`, `Config{Image, Labels, Tty}`, `HostConfig{LogConfig.Type, RestartPolicy, Memory, NanoCpus}` |
| `GET /containers/{id}/logs` | `follow, stdout, stderr, timestamps, since, until, tail` (**`details` refused**) | piped, never buffered |
| `GET /events` | `since, until, filters` | piped; `filters.type` **forced** to `["container"]` |

Everything else is refused (403 off the list, 405 other methods, 400 for an
ambiguous shape) and never reaches Docker.

Rules:
- The raw path is matched segment by segment; `%`, `..`, `//`, `;`, `\`, `#`,
  a trailing `/`, absolute-form, HTTP ≠ 1.1, unknown or repeated query names,
  a body, `Upgrade`, `Transfer-Encoding`, `Expect` and method-override headers
  are refused, never normalised. The request to Docker is rebuilt from the
  parsed values; no client header is forwarded. One request per connection.
- Projection is **deny by default** and typed (`project.py`): `Config.Env`,
  `Cmd`, `Args`, `Entrypoint`, `Path`, `Mounts`, `Health.Log` and any field
  Docker adds later never leave the proxy. A reply that doesn't parse or fit
  the types → 502 (fail closed); a Docker error passes only its `message`.
- Streams are piped with back-pressure (a slow reader stops the proxy reading
  from Docker; ≤ ~256 KiB held per connection); when Medic hangs up, the Docker
  stream is closed. ≤ 32 connections (503 beyond), 10 s to send the request
  head, 10 s for a whole JSON reply, 8 MiB per JSON reply.

## Settings

| Variable | Example |
|---|---|
| `VIGIL_MEDIC_DOCKERPROXY_BIND` | `medic-dockerproxy:8472` (an alias on `medic-private`; not a wildcard) |
| `VIGIL_MEDIC_DOCKERPROXY_SOCKET` | default `/var/run/docker.sock` |

It refuses to start (exit 2) if `AGENT_INTERNAL_TOKEN`, `VIGIL_TOOLS_TOKEN`,
`DAEMON_WEBHOOK_TOKEN`, `VIGIL_MEDIC_API_KEY` or `POSTGRES_PASSWORD` is in its
environment: it needs no credential.

```
python -m services.medic_dockerproxy run
python -m services.medic_dockerproxy check   # exit 0 if /_ping answers 200 through the proxy
```

## Tests

Its own lock (no runtime dependencies; dev only). From the repo root, with the
venv outside `services/` (the repo's ratchets scan every `.py` under it):

```
UV_PROJECT_ENVIRONMENT=../../.venv-medic-dockerproxy uv run --project services/medic_dockerproxy pytest services/medic_dockerproxy
UV_PROJECT_ENVIRONMENT=../../.venv-medic-dockerproxy uv run --project services/medic_dockerproxy ruff check services/medic_dockerproxy
UV_PROJECT_ENVIRONMENT=../../.venv-medic-dockerproxy uv run --project services/medic_dockerproxy ruff format --check services/medic_dockerproxy
UV_PROJECT_ENVIRONMENT=../../.venv-medic-dockerproxy uv run --project services/medic_dockerproxy lint-imports
```

Most tests use a fake Docker on a Unix socket (`tests/conftest.py`; fixtures are
real Docker 29.4 / API 1.54 replies with a planted secret).
`tests/test_live_docker.py` is SP2's 43 against a live engine (C3 check 4 with
the negative control); it skips without a Docker socket and must run in CI.

## Image

`infra/docker/Dockerfile.medic-dockerproxy`: uid/gid 10003, read-only root
filesystem, `HEALTHCHECK` running `check`. Run it with the socket mounted `:ro`
and the socket's group added (`group_add`: the `docker` gid on Linux, 0 on
Docker Desktop). On Compose that gid is `VIGIL_MEDIC_DOCKER_GID`, which
`scripts/medic/enable-compose.sh` finds and writes to `compose.env`; it is the
only name for it.

```
docker build -f infra/docker/Dockerfile.medic-dockerproxy -t vigil-medic-dockerproxy .
```
