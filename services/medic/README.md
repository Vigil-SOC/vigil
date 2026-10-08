# Medic

Medic is the ops-health watcher for a Vigil install (program name: System
Watcher). It watches the install's own health, not security alerts. Phase 0 is
observe-only: no fixes, and nothing leaves the site.

It is **off by default**. `VIGIL_MEDIC_ENABLED=true` turns it on; anything else
and `run` logs why and exits 0.

```
python -m services.medic run     # the service (below)
python -m services.medic check   # exit 0 only if the heartbeat is fresh (< 120 s)
```

`run` works in 15 s cycles: the sensors that are due read (`http_ready` on the
agent worker's `/readyz`), every observation passes the K2 redaction choke, the
engine evaluates the tick, router R1 gives each incident its rule's lane, and the
store's single writer chains the record into `medic.db`. Each cycle then saves
the engine state and writes the heartbeat; a loop that stops cycling for 180 s
is ended by the watchdog. On start, if the last heartbeat shows Medic was
stalled or off, one `gap` record goes into the chain first. Rules are the
dev-mode set in `rules/dev/` (no pack loader yet, F6).

| Setting | Default |
|---|---|
| `VIGIL_MEDIC_ENABLED` | off |
| `VIGIL_MEDIC_DATA_DIR` | `/var/lib/vigil-medic` (macOS: `/Library/Application Support/vigil-medic`) |
| `VIGIL_MEDIC_INSTALL_SHAPE` | `compose` (`start_sh`, `compose` or `helm`) |
| `VIGIL_MEDIC_AGENT_WORKER_ADDR` | `agent-worker:6990` (`start_sh`: `127.0.0.1:6990`); `host:port` only |

Data directory: `medic.db` (+ `-wal`, `-shm`), `medic.lock`, `instance_id`,
`run/heartbeat`, `run/engine-state.json`. `python -m services.medic.store verify`
checks the chain.

## Isolation

Medic imports nothing from `core`, `tools` or any other service, and has its own
`pyproject.toml` and `uv.lock`; `.importlinter` enforces the first. Its runtime
dependencies go in this `pyproject.toml` only, never in the repo's requirements.
If `uv.lock` conflicts after a merge, re-run `uv lock --project services/medic`
rather than hand-merging.

## Tests

One command, from the repo root:

```
UV_PROJECT_ENVIRONMENT=../../.venv uv run --project services/medic pytest services/medic
```

It runs the contract tests (`contracts/tests/`) and Medic's own (`tests/`). The
venv goes in the repo root's `.venv` (gitignored, and unused by Vigil, which
uses `venv/`). Left in `services/medic/.venv`, the repo's ratchet tests, which
scan every `.py` file under `services/`, would read the venv's site-packages.

Lint, the same as the `medic-contracts` CI job:

```
UV_PROJECT_ENVIRONMENT=../../.venv uv run --project services/medic ruff check services/medic
UV_PROJECT_ENVIRONMENT=../../.venv uv run --project services/medic ruff format --check services/medic
UV_PROJECT_ENVIRONMENT=../../.venv uv run --project services/medic lint-imports
```

## Contracts

`contracts/` holds the schemas, fixtures, evaluation vectors, reference
implementations and their tests. They are the spec: a vector or schema that
looks wrong is a contract change, made in its own PR, never a code workaround.

## Image

`infra/docker/Dockerfile.medic`: Medic's venv from this lock, no `core`, uid and
gid 10001, read-only root filesystem, `HEALTHCHECK` running `check`. Mount the
data volume at `/var/lib/vigil-medic`.

```
docker build -f infra/docker/Dockerfile.medic -t vigil-medic .
```
