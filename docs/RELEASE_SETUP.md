# Release Setup

One time setup for Vigil's automated release flow. Day to day releases
need no configuration on this page — see [RELEASING.md](../RELEASING.md)
for the release procedure. This document covers the GitHub App that
`.github/workflows/release-please.yml` depends on.

## Why a GitHub App

`release-please.yml` mints short lived (1 hour) tokens at runtime from a
GitHub App rather than using a long-lived personal access token. The App
is org owned, so releases keep working when individual maintainers
leave, and tokens auto-rotate so there is no manual key rotation chore.

## Required App permissions

Create a GitHub App on the `Vigil-SOC` org (org Owner role required) at
[github.com/organizations/Vigil-SOC/settings/apps](https://github.com/organizations/Vigil-SOC/settings/apps).
Grant only these **Repository permissions** — leave everything else at
"No access":

| Permission     | Access         | Why                                                       |
|----------------|----------------|-----------------------------------------------------------|
| Contents       | Read and write | Push tags and update `VERSION` / `CHANGELOG.md`           |
| Pull requests  | Read and write | Open and update the release PR                            |
| Issues         | Read and write | Link release notes to closed issues                       |
| Workflows      | Read and write | In case the release PR ever touches `.github/workflows/`  |

Uncheck "Active" under Webhooks (not needed). Restrict installation to
"Only on this account". Install the App on the `vigil` repo only.

## Wire it into the repo

`.github/workflows/release-please.yml` references two repo-level
configuration items by exact name. Set both at
[Settings → Secrets and variables → Actions](https://github.com/Vigil-SOC/vigil/settings/secrets/actions):

| Name                         | Tab           | Value                                                                                                                       |
|------------------------------|---------------|-----------------------------------------------------------------------------------------------------------------------------|
| `RELEASE_PLEASE_APP_ID`      | **Variables** | The numeric App ID shown on the App's settings page                                                                         |
| `RELEASE_PLEASE_PRIVATE_KEY` | **Secrets**   | Full contents of the `.pem` file generated under "Private keys" on the App's settings page (including the `BEGIN`/`END` lines) |

See GitHub's docs on
[managing private keys for GitHub Apps](https://docs.github.com/en/apps/maintaining-github-apps/managing-private-keys-for-github-apps)
for the mechanics of generating the `.pem`.

## Failure mode

If either configuration item is missing or empty, the "Mint GitHub App
token" step in `release-please.yml` fails immediately and no release PR
is opened. Symptom: every push to `main` produces a failed
release-please workflow run; no release PR appears in the PR list.
