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

A fix release carries no database migration and no schema change. Every fix merges to `main` first. Nothing merges from `main` into the release branch, or back.

A commit landing on `release/X.Y.x` makes release-please open a release PR on that branch for `X.Y.(Z+1)`. Merging it tags `vX.Y.(Z+1)` and `release.yml` builds it. The release PR bumps only the patch: the workflow adds a `Release-As: X.Y.(Z+1)` commit to the branch, which pins the version whatever `main`'s versioning setting says.

The release branch carries its own `.github/.release-please-manifest.json`, which starts at `X.Y.0`. After a fix release, `main`'s manifest and `VERSION` still read `X.Y.0`; that is expected. Betas are numbered from `main`'s `VERSION`.

CI (the CI/CD Pipeline, and the Helm chart check for chart changes) runs on pushes to and pull requests into `release/**`, as it does for `main`.

### Backporting a fix

Add the `backport` label to a PR that merged into `main`, or label it before it merges: the copy starts when the PR is merged and labeled, whichever happens last. [`backport.yml`](../.github/workflows/backport.yml) then:

- cherry-picks the PR's squash commit onto `release/X.Y.x` with `git cherry-pick -x` (the copy names the original commit, and keeps its title for the release changelog), and opens `Backport #N: <title>` into that branch with the release-please App token, so CI runs on it;
- opens it as a **draft with the conflict markers committed** if the commit does not apply cleanly, and does nothing more. It fails CI by design until someone resolves the conflicts and marks it ready;
- comments on the PR and stops if no release branch exists yet, and stops if a `backport/N-*` branch already exists (the label was added twice).

Only that one commit is copied. A fix that depends on earlier commits that are not on the release branch conflicts; resolve it by hand or leave it on `main`.

[`release-guard.yml`](../.github/workflows/release-guard.yml) runs on every PR into `release/**`, backports or not, and fails when the PR:

- changes anything under `core/upgrade/versions/` or `core/upgrade/head_schema.json` (no migration or schema change in a fix release), or
- changes more than 300 lines, added plus deleted (too big to be a fix).

A revision on `main` also writes its id, alone, into `core/upgrade/versions/HEAD`. Two revisions from the same head change that same line, so the second conflicts until it is rebased onto the new head or carries a merge revision.

Release PRs go through the same check; they only touch the manifest and changelog.

## Repository settings

Not code, so an admin sets them:

- The release-please GitHub App (`RELEASE_PLEASE_APP_ID`, `RELEASE_PLEASE_PRIVATE_KEY`) writes the `Release-As` commit straight to the branch and creates and deletes `release/*` branches. It must be a bypass actor on the rulesets below.
- Add a `release/**` ruleset mirroring the "main branch protection" ruleset: pull requests and CI required for everyone else, the App (installation id as in main's `bypass_actors`) as bypass. Without it the flow still works, but the release branch is unprotected.
- A `backport` label: `gh label create backport -d "Copy this merged fix onto the current release branch"`.
- Label nothing `backport` until the factory's default-branch-only review setting ([vigil-factory#24](https://github.com/Vigil-SOC/vigil-factory/issues/24)) is on. Without it the factory treats the copy PRs as outside PRs, and can close them as already on `main` or merge `main` into them.
- Require the `Release guard` check on the `release/**` ruleset, next to CI.
