# VigilSOC: threat model and severity guide for the scanner

VigilSOC (`Vigil-SOC/vigil`, https://vigilsoc.org, Apache-2.0) is an open-source
AI security operations platform: a FastAPI backend, a React console, an
autonomous SOC daemon, LLM agents (Python and a TypeScript agent service), and
MCP integrations that read from and act on a customer's security stack (SIEM,
EDR, identity, ticketing). Operators self-host it with docker-compose, Helm, or
the Electron desktop app.

Because Vigil holds credentials for an organization's security tools and can
take response actions (isolate a host, change a firewall rule), a bug here is
rarely "just" a web bug: it can become access to, or control over, the
customer's defenses.

This file condenses `/src/SECURITY.md` (Scope section) for a scan. That file is
authoritative where the two differ.

## Where untrusted input enters

Roughly in order of exposure:

- **HTTP API** — `services/api/main.py`, every router under
  `services/api/routers/` and `core/api/`, discovered by
  `services/api/discovery.py`. Authentication and RBAC live in `core/auth/`
  (JWT, cookies, token blacklist, password reset, permissions, MCP credentials,
  tool principals) and `services/api/middleware/` (CSRF and friends).
  Unauthenticated routes (login, `/api/auth/bootstrap`, password reset,
  webhooks) deserve the closest look.
- **Ingested security data** — `core/ingestion/` (SIEM pulls, Kafka consumers,
  webhooks; rejected webhooks in `core/webhook_rejections.py`) and
  `core/federation/`. Alerts, logs and findings are attacker-influenced by
  definition: an adversary on the monitored network controls hostnames,
  command lines, file names and email subjects that flow into Vigil.
- **Documents** — `core/documents/` parses user-attached PDFs, images and
  Office files (liteparse, LibreOffice).
- **LLM / agent layer** — `core/llm/`, `core/agents/`, `services/agent/`,
  `services/daemon/`. Ingested data and documents reach model context, so
  indirect prompt injection is a primary attack path. What matters is whether
  it escalates into capability.
- **MCP integrations** — `core/integrations/<vendor>/`, `core/integrations/mcp/`,
  `tools/mcp/`, `mcp-config.json`, custom integrations
  (`services/api/routers/custom_integrations.py`): spawned server processes,
  outbound HTTP to vendor APIs, and responses from those APIs.
- **Response actions and approvals** — `core/response/`, especially
  `approval_service.py`, and the daemon's autonomy knobs (`INTENT.md`,
  `services/daemon/`).
- **Secrets** — `core/secrets.py`, `core/secrets_manager.py`, the encrypted
  store `~/.vigil/secrets.enc`, Helm secret plumbing.
- **Data layer** — SQLAlchemy models and raw SQL, `infra/database/`, case and
  tenant isolation.
- **Web console** — `clients/web/` (XSS, token storage, cross-user leaks);
  the Electron shell in `clients/desktop/`.
- **Deployment artifacts** — `infra/docker/`, `infra/helm/vigil/`, `scripts/`.

## What we care about most

1. Authentication bypass or privilege escalation (viewer → analyst → admin),
   with `DEV_MODE=false`.
2. Bypass of the human approval gate for world-changing response actions.
3. Prompt injection from ingested data or documents that causes unapproved tool
   execution, secret exfiltration, or access to another tenant's or case's data.
4. Credential disclosure: integration or LLM provider keys in logs, error
   responses, agent context, the database in cleartext, or image layers.
5. SSRF via integration/custom-integration URLs or LLM provider base URLs
   reaching internal services or cloud metadata.
6. Command injection into spawned MCP servers or helper scripts.
7. SQL injection, path traversal, unsafe deserialization, IDOR across cases or
   tenants.
8. Stored XSS in the console from ingested alert fields (attacker-controlled
   text rendered to analysts).
9. LLM cost amplification that defeats the budget guardrails in
   `services/daemon/orchestrator.py` and the budgets API
   (`services/api/routers/budgets.py`).

## Out of scope (do not report)

- Anything that requires `DEV_MODE=true`. It bypasses auth by design, defaults
  to false, and announces itself on startup.
- Default credentials in development material (the Postgres password in
  `infra/docker/docker-compose.yml`, the throwaway `JWT_SECRET_KEY` in
  `.oss-scanner/Dockerfile`), and placeholder values in `env.example`.
- Findings that need a configuration documented as unsafe (port 6987 exposed
  with `DEV_MODE=true`, CSRF report-only in production).
- Vulnerabilities in upstream products Vigil integrates with (Splunk,
  CrowdStrike, VirusTotal, …). Vigil's own client mishandling their
  credentials or responses *is* in scope.
- Scanner-style output with no demonstrated impact: missing headers,
  outdated dependencies with no reachable vulnerable path, self-XSS,
  clickjacking on pages with no sensitive action.
- Volumetric DoS / resource exhaustion (except LLM cost amplification above).
- An agent producing wrong or impolite text without gaining capability.

## Severity

We rate with CVSS v3.1, assuming a production deployment per
https://vigilsoc.org/docs/production-security/ (`DEV_MODE=false`, real JWT
secret, TLS in front, API not exposed to the internet without auth).

- **Critical** — unauthenticated RCE; unauthenticated auth bypass; prompt
  injection from ingested data that executes a response action or exfiltrates
  integration credentials with no human step; reading the secrets store
  without credentials.
- **High** — authenticated low-privilege → admin; approval-gate bypass by an
  authenticated user; cross-tenant / cross-case data access; SSRF to internal
  services or cloud metadata; SQLi reachable by any authenticated user; stored
  XSS from ingested alert data that runs in an analyst's session; credential
  written to logs or API responses.
- **Medium** — post-auth SQLi or command injection reachable only by an admin;
  stored XSS requiring an authenticated author; IDOR leaking non-sensitive
  metadata; CSRF on a state-changing endpoint; guardrail bypass enabling LLM
  cost amplification.
- **Low** — information disclosure of versions or internal paths; issues that
  need an already-compromised admin; hardening gaps with a plausible but
  undemonstrated path.

Findings without a working reproducer are capped at Medium unless the code
path is unambiguous.

## Reports and patches

- Include the endpoint or entry point, the role/auth state required, and a
  reproducer: a pytest test under `tests/security/` or `tests/unit/` is ideal
  (the suite runs offline with
  `pytest tests/unit/ tests/security/ -m "not external_service" --no-cov`).
- Patches should be minimal, follow the existing layering (`lint-imports`,
  `.importlinter`), read config through `core.config` / `core.secrets` rather
  than `os.getenv` (a ratchet test enforces this), and pass `flake8`, `black`
  and `isort` on `services/` and `core/`.
