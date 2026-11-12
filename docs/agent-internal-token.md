# Rotating AGENT_INTERNAL_TOKEN

`AGENT_INTERNAL_TOKEN` is the bearer the agent layer and the API present to each other. This page is the rotation for the install you have. Finish it, or undo it completely, before an upgrade or a rollback ([upgrading](upgrading.md)).

Generate a new value with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Who restarts

Four processes send or accept this bearer:

| Process | Sends | Accepts |
| --- | --- | --- |
| backend | current value | current, and previous when set |
| daemon, when it is running | current value | no |
| agent-serve | current value | current, and previous when set |
| agent-worker | current value | no |

The daemon sends through `core/agents/projections.py`. On Helm it runs only when `daemon.enabled` (the default), as one pod. On the host it runs only under `./start.sh -d`.

`vigil.envFrom` injects every key of the app Secret into each workload that uses it, including `llm-worker`. `llm-worker` does not use this bearer, and Compose does not pass the variable to it. Restart the four processes above, not every workload that can see the Secret.

Senders send `AGENT_INTERNAL_TOKEN` only. The backend (`authorise` in `core/agents/internal_auth.py`) and agent-serve (`authorised` in `services/agent/serve.ts`) also accept `AGENT_INTERNAL_TOKEN_PREVIOUS` when that value is set and different from the current one. An empty previous, or one equal to the current value, is not a second credential. Previous is ignored while the current value is empty: an empty current stays "not configured".

The backend and the daemon read both names through `get_secret`, which returns the encrypted store's value when the key is there and the environment otherwise (`core/secrets.py`). agent-serve and agent-worker read the environment only. A value left in the encrypted store is the one the Python processes keep using after the Secret or `.env` changes. On Helm, an `extraEnv` entry for either name on a workload overrides the Secret for that workload and stays put when the Secret changes.

## What each step does to calls

The overlap is three steps. Do not start the next one until every holder from this step has finished restarting.

1. Set previous to the new value. Leave current as it is. Nothing sends the new value yet, so calls keep using the current one. Acceptors now hold the new value for the swap.
2. Set current to the new value and previous to the old one. A holder still on step 1 and a holder already on step 2 accept both values, so calls between them succeed.
3. Clear previous. Where the store cannot keep an empty key, set previous equal to current: acceptors treat that as unset. After this restart only the new value works.

Restarting agent-serve drops an open chat that does not finish inside its stop grace (120s on Compose, `agentServe.terminationGracePeriodSeconds` on Helm). While agent-serve is down, the backend's and the daemon's run-status reads fail. On Compose that is the whole service; Medic can then open `pipeline.agent-reads-failing` (`for: 2m`).

A call between holders that do not share a value is refused. The refusal line says `wrong credential`.

To undo step 1, clear previous (or set it equal to current) and restart. Current never changed. To undo step 2, put current back to the old value and previous back to the new one, restart, then clear previous and restart again. After step 3 the old value is gone; a further change is a new rotation, or the emergency cut below.

## Helm, when you supply the Secret

This is `secrets.existingSecret` or an ExternalSecret. The chart already injects every key of that Secret, so the rotation is edits to the Secret plus restarts. For an `existingSecret`, edit that Secret's keys. For an ExternalSecret, change the outside store and sync it first, below. It is not a `helm upgrade` while `upgrade.enabled` is true (the default): that upgrade's weight-0 hook scales the release to 0 before it applies ([upgrading](upgrading.md#helm)).

### ExternalSecret

Change the outside store, sync, and confirm the Kubernetes Secret before each restart. The chart's `refreshInterval` defaults to 1h, so reconcile it rather than waiting. The ExternalSecret's name is the Secret's name (`vigil-secrets` for a release named `vigil`):

```bash
kubectl -n vigil annotate externalsecret vigil-secrets \
  force-sync="$(date +%s)" --overwrite
```

`dataFrom` copies the outside secret's keys through. Add `AGENT_INTERNAL_TOKEN_PREVIOUS` there for step 1, and remove it, or set it equal to current, for step 3.

`data` maps one remote key at a time. Create the remote key first, empty or equal to the current token, add the mapping once in a planned upgrade before the first rotation, and keep the mapping. The chart sets no `optional` flag on that remote ref, and a mapping whose remote key is missing fails the whole sync. Step 3 changes the remote value. It does not delete the mapping.

### Restart

Confirm the Secret's two keys are the pair for this step, then restart, then wait. One step at a time. With release `vigil` in namespace `vigil`:

```bash
kubectl -n vigil rollout restart \
  deployment/vigil-backend \
  deployment/vigil-agent-serve \
  deployment/vigil-agent-worker
kubectl -n vigil rollout status deployment/vigil-backend
kubectl -n vigil rollout status deployment/vigil-agent-serve
kubectl -n vigil rollout status deployment/vigil-agent-worker
```

When `daemon.enabled`, each step also restarts the daemon. It is one pod, its stop grace is 60s, and its startup probe allows 300s (`failureThreshold` 30, every 10s):

```bash
kubectl -n vigil rollout restart statefulset/vigil-daemon
kubectl -n vigil rollout status statefulset/vigil-daemon
```

The examples above are for a release named `vigil`. A release name that does not already contain `vigil` is prefixed with the chart name (`prod` gives `prod-vigil-backend`, `prod-vigil-agent-serve`, `prod-vigil-agent-worker`, `prod-vigil-daemon`, and Secret `prod-vigil-secrets`). `fullnameOverride` replaces that whole prefix. When `secrets.existingSecret` is set, that name is the Secret and the ExternalSecret.

## Helm, when the chart renders the Secret

The chart's Secret carries `secrets.agentInternalToken` as `AGENT_INTERNAL_TOKEN` and has no previous key. One `helm upgrade` changes it. There is no overlap.

With `upgrade.enabled` left on, that upgrade is a maintenance window: the weight-0 hook scales the release to 0, then every pod starts on the new value. Set the acknowledgement values [an upgrade requires](upgrading.md#helm). With the hook off (`upgrade.enabled` false), pods roll while old and new are both up, and calls are refused until every holder has restarted because there is no previous value.

A later `helm rollback` restores that revision's value.

## Compose

Compose passes `AGENT_INTERNAL_TOKEN` only, including through `x-agent-env`. It does not pass `AGENT_INTERNAL_TOKEN_PREVIOUS`, so there is no overlap.

Change the value in `.env` and re-up once:

```bash
docker compose --env-file .env -f infra/docker/docker-compose.yml up -d
```

Add `--profile daemon` when the daemon is part of this stack. agent-serve's `stop_grace_period` is 120s. Open chats drop. While agent-serve is down, the backend's and the daemon's run-status reads fail, and Medic's agent-reads incident can open (`pipeline.agent-reads-failing`, `for: 2m`).

The same `up` runs the `backup-pre-upgrade` and `upgrade` one-shots. The agent processes wait on them.

## Host (`start.sh`)

The same three steps as a Secret you supply. The processes take the environment directly: `./start.sh` exports `.env`, and `scripts/agent_up.sh` (which start.sh uses for agent-serve and agent-worker) keeps an `AGENT_INTERNAL_TOKEN_PREVIOUS` already in that environment. Run `agent_up.sh` on its own and it reads `AGENT_INTERNAL_TOKEN` from `.env` and does not read the previous key from the file, so the overlap never reaches agent-serve.

Put both keys in `.env` for the step, stop, and start again so the holders come up on that pair. `./start.sh -d` is the start that includes the daemon. Without `-d` the daemon is not running and is not a holder.

```bash
./shutdown_all.sh
./start.sh -d
```

`./start.sh -d` exits if a backend is already running, which is why the stop comes first. Foreground `./start.sh` does not make that check; stop first there too, or a second API process starts.

## Emergency

Any install, including one stopped between steps: set current to a new value and clear previous in that one restart, upgrade, or re-up. On Compose this is the ordinary re-up, since previous is never passed. On a chart-rendered Secret it is the ordinary upgrade, since the Secret has no previous key.

Calls between holders that have restarted and holders that have not are refused until every holder has restarted. The refusal line says `wrong credential`.

## Desktop

Nothing to do. The desktop app generates the value and passes it to its own stack.
