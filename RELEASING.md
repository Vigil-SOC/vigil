# Releasing Vigil

How Vigil is versioned and released.

## Versioning Policy

Vigil follows [Semantic Versioning](https://semver.org/).

- **`0.x.y` (current)** — Pre-stable. `feat:` commits bump the minor
  (`0.1.0` → `0.2.0`) and may include breaking changes to agent prompts,
  workflow schemas, MCP integration interfaces, environment variables, or
  API shapes. `fix:` commits bump the patch (`0.1.0` → `0.1.1`) and are
  always backward-compatible.
- **`1.0.0` and later** — Stable. Breaking changes require a major bump.
  Vigil ships `1.0.0` once the agent and workflow schemas are considered
  stable enough to commit to.

## What Gets Versioned

| File                          | Field                              | Managed by release-please     |
|-------------------------------|------------------------------------|-------------------------------|
| `VERSION`                     | (whole file)                       | yes                           |
| `helm/vigil/Chart.yaml`       | `appVersion`                       | yes                           |
| `frontend/package.json`       | `version`                          | yes                           |
| `frontend/package-lock.json`  | `version` (root + `packages['']`)  | yes                           |
| `helm/vigil/Chart.yaml`       | `version`                          | **no** — bump manually in PR  |

See "Chart version vs appVersion" below for why the chart's `version` is
managed separately.

The Python backend reads `VERSION` directly at import time (see
`backend/__init__.py`), so the FastAPI app version, the
`backend.__version__` attribute, and the `/health` endpoint's `version`
field all stay in sync with `VERSION` automatically. No release-please
configuration is needed for the backend.

## How a Release Happens

Releases are driven by [release-please](https://github.com/googleapis/release-please).
Maintainers do not hand-bump versions or hand-tag releases — both are
automated.

1. Every push to `main` runs `.github/workflows/release-please.yml`.
2. release-please reads commits since the last tag and decides the next
   version from [Conventional Commit](https://www.conventionalcommits.org/)
   prefixes in commit messages on `main` (for squash-merged PRs, the PR
   title becomes the commit — maintainers should adjust PR titles to
   match the convention before squashing):

   - `fix: ...` → patch bump (`0.1.0` → `0.1.1`)
   - `feat: ...` → minor bump (`0.1.0` → `0.2.0`)
   - `feat!: ...` or commits with `BREAKING CHANGE:` in the body → minor
     bump while in `0.x`, major bump from `1.0.0` onward
   - `docs:`, `chore:`, `refactor:`, `test:`, `perf:` appear in the
     changelog but don't trigger a bump on their own
   - Commits with no recognized prefix go under "Other" and don't
     contribute to version selection
3. It opens (or updates) a single **release PR** titled
   `chore(main): release X.Y.Z`. The PR bumps `VERSION`,
   `Chart.yaml` `appVersion`, `frontend/package.json` `version`, and
   updates `CHANGELOG.md`.
4. The release PR stays open and accumulates more commits as they merge.
   This is the grouping mechanism — every commit since the last release
   lives in one PR until you ship.
5. When ready, a maintainer merges the
   release PR. The merge causes release-please to push tag `vX.Y.Z` and
   create a GitHub Release.
6. The tag push triggers `.github/workflows/release.yml`, which builds
   and publishes artifacts.

The only human decision per release is **when to merge the release PR**.

## Chart Version vs appVersion

The Helm chart's `version:` (chart packaging version) is independent of
`appVersion:` (the Vigil release the chart deploys). release-please only
manages `appVersion`. Bump chart `version` manually in your PR when the
chart itself changes — templates, values schema, dependencies.

| Change                              | `appVersion` | chart `version` |
|-------------------------------------|--------------|-----------------|
| New Vigil release, no chart change  | bumps        | unchanged       |
| Helm template fix, no app change    | unchanged    | bump manually   |
| Both at once                        | bumps        | bump manually   |

## GitHub App Setup

The automated flow above depends on a GitHub App (referenced as
`vars.RELEASE_PLEASE_APP_ID` and `secrets.RELEASE_PLEASE_PRIVATE_KEY` in
`release-please.yml`) that mints short-lived tokens at runtime, no
long-lived PATs, no per maintainer ownership. The App is **one time
setup per fork**; once installed, day to day releases need no further
configuration here.

For initial setup or recreating the App, see
[docs/RELEASE_SETUP.md](docs/RELEASE_SETUP.md), covers required
permissions, App creation, `.pem` generation, and where to put the App
ID and private key.

## Manual Release (fallback)

If release-please is broken or unavailable, cut a release by hand. The
manual flow must update **everything** release-please normally manages,
including the manifest file that tracks the last-released version. If
the manifest is not bumped, the next automated run reads its stale
value, thinks the manual release never happened, and opens a release PR
that overwrites it.

1. Open a PR bumping the version to the new release (e.g. `0.2.0`) in
   all of the following:
   - `VERSION`
   - `helm/vigil/Chart.yaml` `appVersion`
   - `frontend/package.json` `version`
   - `frontend/package-lock.json` — both `$.version` and
     `$.packages[''].version`
   - `.github/.release-please-manifest.json` — set the `"."` entry to
     the new version
   - `CHANGELOG.md` — prepend a section with the new version, date, and
     a summary of changes since the previous tag
2. Merge the PR.
3. Tag and push:
   ```bash
   git checkout main && git pull
   git tag -s v0.2.0 -m "Release v0.2.0"
   git push origin v0.2.0
   ```
4. The tag push triggers `release.yml` (build, scan, deploy).
5. **Manually create the GitHub Release** at
   [github.com/Vigil-SOC/vigil/releases/new](https://github.com/Vigil-SOC/vigil/releases/new),
   selecting the tag you just pushed. release-please normally creates
   this; in manual mode nothing else will. Use the `CHANGELOG.md`
   section you wrote as the Release body.

## Future Improvements

Tracked separately, not part of the current release flow:

- **Hotfix branches** — once `1.0+` is out and we need to ship fixes for
  older majors, document a `release/X.Y` branch procedure. Not relevant
  while in `0.x` (just ship a patch from `main`).
- **Re-deploy an old tag** — add a `workflow_dispatch` trigger with a
  `tag` input to `release.yml`, so a previously-released version can be
  rebuilt without moving the tag.
