# Security Policy

Vigil is a security platform. We hold ourselves to the standard we ask of the
tools we integrate with: report vulnerabilities privately, get a human response
quickly, and coordinate disclosure so operators can patch before details are
public.

The machine-readable version of this policy is published per
[RFC 9116](https://www.rfc-editor.org/rfc/rfc9116) at
**<https://vigilsoc.org/.well-known/security.txt>**.

---

## Supported Versions

Vigil is pre-1.0. Security fixes land on `main` and ship in the next release —
we do not backport to earlier minors.

| Version | Supported | Notes |
|---------|-----------|-------|
| Latest `0.x` minor (currently `0.5.x`) | ✅ | Fixes ship here. Upgrade before reporting a bug you can only reproduce on an older tag. |
| Older `0.x` minors | ❌ | No backports. While in `0.x`, minor bumps may break agent prompts, workflow schemas, and MCP interfaces — see [contributing](https://vigilsoc.org/docs/contributing/#versioning-and-releases). |
| `main` (unreleased) | ✅ | Reports welcome; note the commit SHA. |

Container images (`ghcr.io/vigil-soc/vigil-backend`, `vigil-daemon`,
`vigil-agent`) and the Helm chart at `infra/helm/vigil/` follow the same window.
Chart `version` and `appVersion` move in lockstep, so the chart version
identifies the app release it deploys.

Release images are signed keyless by `.github/workflows/release.yml` (GitHub
OIDC → Fulcio, Rekor). Confirm an image came from that workflow — the identity
is pinned to this file and issuer, not any Actions run in the repo. Image tags
drop the leading `v`; the certificate identity uses the git tag:

```bash
cosign verify \
  --certificate-identity https://github.com/Vigil-SOC/vigil/.github/workflows/release.yml@refs/tags/v<version> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/vigil-soc/vigil-backend:<version>
```

The same invocation works for `vigil-daemon` and `vigil-agent`.

---

## Reporting a Vulnerability

**Do not open a public GitHub issue, pull request, or discussion for a security
vulnerability.** Do not disclose it on social media, a blog, or a conference
talk before the coordination window below has closed.

Use whichever channel fits you — both reach the same maintainers.

### 1. GitHub private vulnerability reporting (preferred)

**<https://github.com/Vigil-SOC/vigil/security/advisories/new>**

This is the fastest path. It gives us a private fork to develop and review the
fix in, keeps the whole thread with the eventual advisory, and lets us request a
CVE and credit you in one step. You need a GitHub account; nothing else.

### 2. Email

**<security@deeptempo.ai>** — the point of contact for anything that should not
or cannot go through the GitHub advisory process: you have no GitHub account,
the report concerns Vigil's hosted or website infrastructure rather than this
repository, you need to reach a human about an in-flight report, or GitHub
itself is the wrong venue for what you found.

If you need to encrypt, say so in a first message with no technical detail and
we will reply with a key. Please do not send unsolicited encrypted mail to an
address you have not confirmed we can read.

### What to include

A report we can reproduce is a report we can fix in days instead of weeks:

- **Impact** — what an attacker gains (auth bypass, RCE, secret disclosure,
  privilege escalation, SSRF via an integration, prompt injection into an agent
  with tool access, etc.).
- **Affected component** — path and, ideally, line: `services/api/`,
  `core/llm/harness/`, an MCP server under `core/integrations/<vendor>/`,
  `clients/web/`, an `infra/database/init/` migration, the Helm chart.
- **Version** — release tag, commit SHA, or image digest.
- **Reproduction** — minimal steps, a request/response pair, a curl invocation,
  a proof-of-concept, or a failing test. Include your `DEV_MODE` setting: with
  `DEV_MODE=true` all authentication is bypassed by design, and findings that
  depend on it are not vulnerabilities (see [Out of scope](#out-of-scope)).
- **Deployment shape** — docker-compose, Helm, desktop app, or bare `uvicorn`.
- **Attribution** — how you want to be credited, or that you prefer to stay
  anonymous.

Please keep your testing to your own deployment. Do not test against
`vigilsoc.org`, another organization's Vigil instance, or any third-party
service an integration talks to.

---

## What Happens Next

| Stage | Target |
|-------|--------|
| Acknowledgment that a human has your report | **3 business days** |
| Triage: confirmed or rejected, with severity | **7 business days** |
| Fix or documented mitigation for critical/high | **30 days** from triage |
| Fix or documented mitigation for medium/low | Next scheduled release |
| Public advisory | Coordinated with you — see below |

These are targets, not contractual guarantees; Vigil is maintained by a small
team. We will tell you when something is going to slip rather than let the
thread go quiet.

Throughout, we will: confirm receipt, tell you whether we accept the report and
at what severity, keep you updated as the fix moves, and let you review the
advisory text and your credit line before it is published.

We ask you to: give us a reasonable window before disclosing, not access or
modify data that isn't yours, and tell us if you believe the issue is being
exploited in the wild — that changes our timeline immediately.

We use [CVSS v3.1](https://www.first.org/cvss/calculator/3.1) to set severity.
Our rating is the one that drives our timeline, and we will explain it if it
differs from yours.

---

## Coordinated Disclosure

Our default window is **90 days** from triage to public disclosure, or the day
the fix ships — whichever comes first. We publish sooner when a fix is ready
sooner, and we would rather ship early than sit on a patch.

We will ask to extend past 90 days only when a fix is genuinely hard (a schema
migration, a breaking interface change, an upstream dependency we don't
control), and we will say why. If we go quiet for 90 days with no fix and no
explanation, disclose — that is our failure, not yours.

Fixes are published as
[GitHub Security Advisories](https://github.com/Vigil-SOC/vigil/security/advisories)
on the upstream repository. For anything of medium severity or above we request
a CVE through GitHub, note the advisory in [CHANGELOG.md](CHANGELOG.md), and
name the affected and fixed versions so operators can tell whether they need to
act. Watch that page, or the repository's releases, if you run Vigil in
production.

---

## Safe Harbor

If you make a good-faith effort to follow this policy, we will not pursue or
support legal action against you, and we consider your research authorized
under the Computer Fraud and Abuse Act and equivalent laws. If a third party
brings action against you for research that followed this policy, we will say
publicly that it was authorized.

Good faith means: you test only against deployments you own or have written
permission to test, you stay within the scope below, you avoid privacy
violations, data destruction, and any degradation of service, you access only
the minimum data needed to demonstrate the issue, you delete any retained data
once the report is closed, and you report promptly and give us the coordination
window.

This is not a paid bug bounty. Vigil is an Apache-2.0 open-source project with
no bounty budget; we pay in credit, in
[acknowledgment](#acknowledgments--hall-of-fame), and in fixing what you found.

---

## Scope

### In scope

Anything in this repository that a deployment actually runs:

- **Authentication and authorization** — JWT handling in `core/`, the
  middleware under `services/api/middleware/`, RBAC, session and cookie
  handling, token revocation, CSRF.
- **API surface** — every router discovered by `services/api/discovery.py`:
  injection, SSRF, path traversal, IDOR, missing authorization, mass
  assignment, unsafe deserialization.
- **The agent and LLM layer** — `core/llm/`, `core/agents/`. Prompt injection
  that escalates into real capability (unapproved tool execution, exfiltration
  of secrets or another tenant's case data, bypass of the response-approval
  gate) is in scope. Getting an agent to produce impolite or wrong text is not.
- **MCP integrations** — `core/integrations/<vendor>/`, `tools/mcp/`,
  `mcp-config.json`, and `core/integrations/mcp/service.py`: credential
  leakage into logs or agent context, command injection into a spawned
  server, TLS
  verification that can be silently disabled, and bypass of the
  approval workflow in `core/response/approval_service.py` for actions that
  change the world (host isolation, firewall rules).
- **Secret handling** — the encrypted store at `~/.vigil/secrets.enc`,
  `core.secrets`, Helm secret plumbing, and anything that writes a credential
  to a log, an error response, or the database in cleartext.
- **Data layer** — SQL injection, the `infra/database/init/` migrations, and
  tenant or case isolation failures.
- **Web client** — `clients/web/`: XSS, CSRF, token storage, and anything that
  leaks one user's data to another.
- **Deployment artifacts** — the Dockerfiles and compose file under
  `infra/docker/`, the Helm chart, and `scripts/`: privilege escalation inside
  a container, a chart default that exposes an unauthenticated service, a
  secret baked into an image layer.
- **Supply chain** — a dependency with a known exploitable CVE reachable from
  Vigil's own code paths, or a compromised build or release step.

### Out of scope

Not vulnerabilities. Reports on these will be closed with a pointer back here:

- **`DEV_MODE=true` bypassing authentication.** That is its documented purpose.
  It defaults to `false` in `core/config.py`, and `env.example` ships `true`
  only for local development. See [DEV_MODE](https://vigilsoc.org/docs/dev-mode/).
- **Default credentials in development material** — the `admin` / `admin123`
  dev login and the default PostgreSQL password in
  `infra/docker/docker-compose.yml`. Both are documented as
  must-change-before-production in
  [production security](https://vigilsoc.org/docs/production-security/).
- **Placeholder values in `env.example`.** They are a template, not a
  configuration.
- **Findings that require a misconfiguration we already document as unsafe** —
  for example exposing port 6987 to the internet with `DEV_MODE=true`, or
  running with the report-only CSRF switch turned on in production. If you find
  a case [production security](https://vigilsoc.org/docs/production-security/) does *not*
  cover, that gap is worth reporting.
- **Vulnerabilities in an upstream product Vigil integrates with** (Splunk,
  CrowdStrike, VirusTotal, and the rest). Report those to that vendor. If
  Vigil's *client* for one is what mishandles the credential or the response,
  that is in scope.
- **Automated scanner output with no demonstrated impact**, missing
  hardening headers with no exploit path, best-practice advice, outdated
  dependencies with no reachable vulnerability, and self-XSS or clickjacking on
  pages with no sensitive action.
- **Denial of service through resource exhaustion or volumetric traffic**, and
  social engineering of maintainers or users. LLM cost-amplification that
  defeats the guardrails in `services/daemon/orchestrator.py` *is* in scope —
  it costs operators real money.

---

## Hardening Your Deployment

Most incidents we would expect to see are configuration, not code. Before you
run Vigil anywhere real:

- **[Production security](https://vigilsoc.org/docs/production-security/)** — the
  auditable checklist of every security-relevant switch and its production
  value. Start here.
- **[DEV_MODE](https://vigilsoc.org/docs/dev-mode/)** — what the auth bypass does and why it must
  never be enabled in production.
- **[State and secrets](https://vigilsoc.org/docs/state/)** — where secrets live, and why provider
  keys and integration credentials belong in the UI and the encrypted store
  rather than in `.env`.
- **[Helm secrets](https://vigilsoc.org/docs/helm-secrets/)** — secret management for
  Kubernetes deployments.
- **[Deployment guide](https://vigilsoc.org/docs/deployment/)** — network exposure,
  TLS termination, and reverse-proxy placement.

Non-negotiables: set `DEV_MODE=false`, generate a real `JWT_SECRET_KEY`, change
the default database password, terminate TLS in front of the API, and never
commit a credential.

---

## Security in Development

Contributions are held to the same bar, enforced in CI (`ci-cd.yml`, plus the
nightly audit in `nightly.yml`):

- `bandit` for Python and `npm audit` for the web client.
- Trivy scans of released container images.
- Dependabot for dependency updates (`.github/dependabot.yml`).
- Ratchet tests that fail the build on ambient state — new `os.getenv` calls
  must go through `core.config` or `core.secrets`
  (`tests/unit/_ratchets/`), which is what keeps credentials out of
  scattered env reads.
- `lint-imports` enforcing the layering in `.importlinter`.

If you are fixing a reported vulnerability, coordinate with the maintainers
**before** opening a public pull request — a PR that describes the bug
discloses it. We will work with you in the private advisory fork instead.

---

## Alignment with `security.txt`

This document is the human-readable half of the policy published at
<https://vigilsoc.org/.well-known/security.txt>. The two must agree; when they
disagree, this file is authoritative and `security.txt` should be corrected.

| `security.txt` field | Corresponds to |
|---------------------|----------------|
| `Contact` (advisory URL) | [GitHub private vulnerability reporting](#1-github-private-vulnerability-reporting-preferred) |
| `Contact` (`mailto:`) | [security@deeptempo.ai](mailto:security@deeptempo.ai) — see [Email](#2-email) |
| `Policy` | This file |
| `Acknowledgments` | [Acknowledgments & Hall of Fame](#acknowledgments--hall-of-fame) |
| `Preferred-Languages` | `en` |
| `Canonical` | `https://vigilsoc.org/.well-known/security.txt` |
| `Expires` | Reviewed and re-signed before it lapses |

**Maintainers:** any change to a contact address, the disclosure window, or the
section headings this table anchors to must be mirrored in `security.txt` in
the same change. The `Acknowledgments` field points at the
`#acknowledgments--hall-of-fame` anchor below, so renaming that heading breaks
the published link. Refresh `Expires` on every review — an expired
`security.txt` is treated as an unmaintained policy.

---

## Acknowledgments & Hall of Fame

Researchers who have reported valid vulnerabilities to Vigil and coordinated
disclosure with us. We add you here when the advisory publishes, with the name
or handle you asked for — or not at all, if you would rather stay anonymous.

<!-- Add entries newest first: - **Name / handle** — short description of the issue (advisory GHSA-xxxx, YYYY-MM) -->

_No entries yet. This section is intentionally empty, not neglected — Vigil is
young. Be the first._

Thank you for helping keep Vigil, and the teams who run it, safe.
