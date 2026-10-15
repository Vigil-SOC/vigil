# Accounts for the live connector checks

One free account per connector below. Add the secrets to the repo (Settings, Secrets and variables, Actions). The workflow maps each `LIVE_*` secret to the env var in the "Env var" column. Until a secret exists its probe is skipped as not configured. Probes are in `test_free_tier.py`.

CI runner needs: Node 20+ (`npx`), `uv` (`uvx`), and `git` (uvx installs from git tags).

## API-key connectors

| Connector | Free plan | Create | Env var <- secret | Free-tier limits |
|---|---|---|---|---|
| virustotal | https://www.virustotal.com/gui/join-us (VirusTotal Community) | Profile menu, API key. Nothing to scope. | `VIRUSTOTAL_API_KEY` <- `LIVE_VIRUSTOTAL_API_KEY` | Public API: 4 requests/min, 500/day. Its terms bar commercial use and "business workflows that do not contribute new files"; a weekly one-request check is the whole footprint, but confirm that is acceptable to you before relying on it. |
| shodan | https://account.shodan.io/register | Account page, API key. | `SHODAN_API_KEY` <- `LIVE_SHODAN_API_KEY` | Free key: 1 request/s; search filters and paging cost query credits (a free key has none), which the probe avoids. The probe (`dns_lookup`) has been checked to 401 without a valid key, but not run with a real free key. |
| firecrawl | https://www.firecrawl.dev/pricing (Free) | Dashboard, API keys. | `FIRECRAWL_API_KEY` <- `LIVE_FIRECRAWL_API_KEY` | 1,000 credits/month, no card; the probe scrapes one page (1 credit). 10 scrapes/min. |
| pagerduty | https://www.pagerduty.com/pricing/incident-management/ (Free: 5 users, API included) | My Profile, User Settings, Create API User Token (a user token, not an account REST key). Prefer a dedicated read-only-ish user (Observer, if the Free plan offers it): the token carries that user's rights, and the shipped entry runs with `--enable-write-tools`. | `PAGERDUTY_API_KEY` <- `LIVE_PAGERDUTY_API_KEY` | Probe reads the token owner (`/users/me`); nothing else to set up. |
| okta | Okta Integrator Free Plan, https://developer.okta.com/signup/ | Sign in as a user holding the Read-only Administrator role (Security, Administrators), then Security, API, Tokens, Create token. Tokens take the creating user's role. | `OKTA_DOMAIN` <- `LIVE_OKTA_DOMAIN` (host only, e.g. `integrator-123456.okta.com`); `OKTA_API_TOKEN` <- `LIVE_OKTA_API_TOKEN` | Org expires after 180 days of inactivity (the weekly run keeps it alive). Low API rate limits. |
| cloudflare | https://dash.cloudflare.com/sign-up (Free plan) | Manage Account, Account API tokens, custom token with one permission: Account, Account Firewall Access Rules, Read. Also copy the Account ID (dashboard, account home, menu). | `CLOUDFLARE_API_TOKEN` <- `LIVE_CLOUDFLARE_API_TOKEN`; `CLOUDFLARE_ACCOUNT_ID` <- `LIVE_CLOUDFLARE_ACCOUNT_ID` | Free. A new account has no IP access rules, so the probe expects `success: true` with `rule_count` 0; it fails on any API error. No zones or domains needed. |


## Cloud connectors (OIDC, no stored keys)

Both need the workflow job to have `permissions: id-token: write`.

### aws-security

- Account: any AWS account. The probe is one IAM Access Analyzer `ListAnalyzers` call, which is free.
- IAM: add GitHub as an OIDC identity provider (`token.actions.githubusercontent.com`, audience `sts.amazonaws.com`). Create a role whose trust policy allows `sts:AssumeRoleWithWebIdentity` with `token.actions.githubusercontent.com:sub` limited to this repo, and attach the AWS-managed `SecurityAudit` policy. If the probe reports AccessDenied for `access-analyzer:ListAnalyzers`, add that action inline.
- Secret: `LIVE_AWS_ROLE_ARN`, used as `role-to-assume` in `aws-actions/configure-aws-credentials` (with `aws-region`, e.g. `us-east-1`).
- Env: that action exports `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION`. These are not enough for the server by themselves: it opens every session with `boto3.Session(profile_name=...)`, which ignores env credentials, and `mcp-config.json` gates the entry on `AWS_PROFILE`. The probe therefore writes the exported credentials into a throwaway shared-credentials profile (`vigil-live`) and sets `AWS_PROFILE` itself. If the workflow already sets `AWS_PROFILE`, the probe leaves it alone.

### gcp-scc

- Account: a Google Cloud organization (Cloud Identity Free is enough) with Security Command Center Standard activated. Standard is free and is activated per organization. Create or pick one project in it.
- Workload Identity Federation: create a workload identity pool and an OIDC provider (issuer `https://token.actions.githubusercontent.com`, attribute condition `assertion.repository == '<org>/vigil'`); create a service account; allow `principalSet://.../attribute.repository/<org>/vigil` to impersonate it (`roles/iam.workloadIdentityUser`); grant it `roles/securitycenter.findingsViewer` on the organization or project.
- Secrets: `LIVE_GCP_WORKLOAD_IDENTITY_PROVIDER` (full provider resource name), `LIVE_GCP_SERVICE_ACCOUNT` (its email), `LIVE_GCP_SCC_PROJECT_ID` (the project id; also the env var the probe reads).
- Workflow: `google-github-actions/auth` with `workload_identity_provider` and `service_account`; it exports `GOOGLE_APPLICATION_CREDENTIALS`, which the probe requires and the server's Application Default Credentials pick up.
- The probe lists the project's top vulnerability findings. A project with none returns an empty list, which passes: it proves auth and the SCC API, not that findings exist. Not verified against a real organization.

## Cannot be tested in CI

- **jira** (Atlassian Rovo MCP). As configured, `mcp-config.json` runs `mcp-remote` against `https://mcp.atlassian.com/v1/mcp` with no header, which starts a browser OAuth 2.1 flow; a CI runner cannot complete it. Atlassian does document headless auth for this server: a personal API token sent as `Authorization: Basic base64(email:api_token)`, or a service-account key as `Authorization: Bearer <key>`, on `https://mcp.atlassian.com/v2/mcp`. It works only if an org admin turns on API token auth (Atlassian Administration, Rovo, Rovo MCP server, Authentication), and the token needs a `cloudId` passed per request. Using it means changing the entry (add `--header "Authorization: ..."` with a secret, move to v2), then a free Atlassian site could back a probe. Whether the free plan includes the Rovo MCP server is not stated in Atlassian's docs. Not done here because it changes the shipped connector.
- **loglm** (DeepTempo LogLM connector). A paid, opt-in connector with no public free tier or public docs, and the repo holds no reference to its MCP tool names (only `LOGLM_MCP_URL` / `LOGLM_MCP_TOKEN` plumbing), so there is no tool to probe. It could run against a DeepTempo-operated staging connector: provide `LOGLM_MCP_URL` and `LOGLM_MCP_TOKEN` as `LIVE_LOGLM_MCP_URL` / `LIVE_LOGLM_MCP_TOKEN` and name one read-only tool, then add a `Probe("loglm", ...)`.
