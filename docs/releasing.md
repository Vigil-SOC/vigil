# Releasing

Releases are cut by [release-please](https://github.com/googleapis/release-please) from two kinds of branch. [`release.yml`](../.github/workflows/release.yml) builds, signs and publishes the images for any `vX.Y.Z` tag, whichever branch it was cut from. Image tags are described in the [README](../README.md#quick-start).

| Branch | Releases | How |
|---|---|---|
| `main` | Minors (`1.2.0`, `1.3.0`, …) and majors | A maintainer merges `main`'s release PR |
| `release/X.Y.x` | Fix releases (`X.Y.1`, `X.Y.2`, …) | A maintainer merges that branch's release PR |

A merge to `main` makes no release. Daily betas of `main` are a separate path ([`beta.yml`](../.github/workflows/beta.yml)).

## A minor release, by hand

`main` always has one open release PR, titled for the next minor (`chore(main): release 1.3.0`). It collects the changelog as PRs merge and stays open until a maintainer decides it is time. To release, merge it. That tags `v1.3.0`, which:

1. runs `release.yml` to build and publish the images, and
2. runs [`release-branch.yml`](../.github/workflows/release-branch.yml), which creates `release/1.3.x` at the tag's commit and deletes every other `release/*` branch.

There is exactly one release branch at a time, so the previous line (`release/1.2.x`) stops getting fixes the moment `1.3.0` ships. Until the first `X.Y.0` after this flow landed, no release branch exists.

For a major, run the **release-please** workflow from `main` with `major` (**Actions → release-please → Run workflow**), then merge the PR it opens or updates.

## Fix releases

A fix release carries no database migration and no schema change. Every fix merges to `main` first; how it then reaches the release branch is a separate step (backport). Nothing merges from `main` into the release branch, or back.

A commit landing on `release/X.Y.x` makes release-please open a release PR on that branch for `X.Y.(Z+1)`. Merging it tags `vX.Y.(Z+1)` and `release.yml` builds it. The release PR bumps only the patch: the workflow adds a `Release-As: X.Y.(Z+1)` commit to the branch, which pins the version whatever `main`'s versioning setting says.

The release branch carries its own `.github/.release-please-manifest.json`, which starts at `X.Y.0`. After a fix release, `main`'s manifest and `VERSION` still read `X.Y.0`; that is expected. Betas are numbered from `main`'s `VERSION`.

CI (the CI/CD Pipeline, and the Helm chart check for chart changes) runs on pushes to and pull requests into `release/**`, as it does for `main`.

## Repository settings

Not code, so an admin sets them:

- The release-please GitHub App (`RELEASE_PLEASE_APP_ID`, `RELEASE_PLEASE_PRIVATE_KEY`) writes the `Release-As` commit straight to the branch and creates and deletes `release/*` branches. It must be a bypass actor on the rulesets below.
- Add a `release/**` ruleset mirroring the "main branch protection" ruleset: pull requests and CI required for everyone else, the App (installation id as in main's `bypass_actors`) as bypass. Without it the flow still works, but the release branch is unprotected.
