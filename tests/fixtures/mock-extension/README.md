# Mock LogLM connector (page-extension test fixture)

A minimal stand-in for the real LogLM connector BFF, so Vigil's page-extension
host (`frontend/src/redesign/extensions`) and the session-token endpoint
(`backend/api/extensions.py`) can be tested end to end without the connector.

It serves the four contracts Vigil consumes:

| Route | Purpose |
|-------|---------|
| `GET /manifest.json` | spec-shaped manifest (nav label, route, element tag, bundle URL, permission, gate) |
| `GET /assets/loglm-admin.js` | the ESM web-component bundle (vanilla-JS stub, Shadow DOM) |
| `POST /session` | mint-secret-gated; returns a fake short-lived session token |
| `GET /model-performance` | bearer-gated; returns `model_performance.json` |

## Run

```bash
python tests/fixtures/mock-extension/server.py --port 8099
```

## Wire it into Vigil (DEV_MODE)

1. Start the mock (above), the backend, and the frontend.
2. Settings → Integrations → **LogLM** → enable, then set:
   - **Connector URL** = `http://localhost:8099`
   - **Session Signing Secret** = `dev-mock-mint-secret` (matches `MINT_SECRET` in `server.py`)
3. A **LogLM** tab appears in the nav rail. Open it: the mock element mounts
   inside the console, themes itself from the injected host context, and loads
   the fixture metrics. The buttons emit `notify` / `setViewFull` / `error`
   events to exercise the host's event relay.
4. Stop the mock → the LogLM page degrades to "unavailable" while the rest of
   the console keeps working (proves isolation).
