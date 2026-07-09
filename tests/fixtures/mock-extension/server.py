"""Mock LogLM connector BFF — for testing Vigil's page-extension host.

Stands in for the real connector so the frontend ExtensionHost + the backend
session-token endpoint can be exercised end to end without the connector.

Serves the four contract surfaces Vigil consumes:
  GET  /manifest.json         spec-shaped extension manifest
  GET  /assets/loglm-admin.js the ESM web-component bundle (vanilla-JS stub)
  POST /session               mint-secret-gated; returns a fake session token
  GET  /model-performance     bearer-gated; returns fixture metrics

Usage:
  python tests/fixtures/mock-extension/server.py [--port 8099]

Then in Vigil (DEV_MODE): Settings -> Integrations -> LogLM, enable it with
  connectorUrl = http://localhost:8099
  Session Signing Secret = dev-mock-mint-secret   (matches MINT_SECRET below)
Open the "LogLM" tab in the console; the mock element mounts, themes itself
from the host context, loads the fixture metrics, and can toast/navigate the
host via the buttons.
"""

import argparse
import json
import pathlib

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

HERE = pathlib.Path(__file__).parent
ELEMENT_TAG = "loglm-admin"
BUNDLE_FILE = "loglm-admin.js"
# The shared mint secret the Vigil backend presents on POST /session. Enter
# this same value as the integration's "Session Signing Secret" in dev.
MINT_SECRET = "dev-mock-mint-secret"

app = FastAPI(title="Mock LogLM connector")
# Browser talks to this BFF directly (cross-origin), so CORS is required.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/manifest.json")
def manifest():
    return {
        "id": "loglm",
        "name": "LogLM",
        "version": "1.0.0",
        "hostApiVersion": "1.x",
        "render": {
            "mode": "element",
            # relative — Vigil resolves it against connectorUrl
            "bundleUrl": f"/assets/{BUNDLE_FILE}",
            "elementTag": ELEMENT_TAG,
        },
        "mountPoints": [
            {
                "type": "screen",
                "key": "loglm",
                "icon": "brain",
                "navLabel": "LogLM",
                "title": "LogLM Detections",
                "subtitle": "Model status, tuning & suppression",
                "permission": "loglm.view",
                "gate": {"integration": "loglm"},
            }
        ],
    }


@app.get(f"/assets/{BUNDLE_FILE}")
def bundle():
    return Response(
        (HERE / BUNDLE_FILE).read_text(),
        media_type="application/javascript",
    )


@app.post("/session")
def session(authorization: str = Header(None)):
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token != MINT_SECRET:
        raise HTTPException(status_code=401, detail="invalid mint secret")
    return {"token": "mock-session-token", "expires_in": 900}


@app.get("/model-performance")
def model_performance(authorization: str = Header(None)):
    if not (authorization or "").startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing session token")
    return JSONResponse(json.loads((HERE / "model_performance.json").read_text()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
