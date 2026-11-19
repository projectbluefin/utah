---
name: ci-workflows
version: "1.4"
last_updated: "2026-11-07"
id: ci-workflows
one_line_purpose: Navigate Utah's build, promote, and sync workflow topology.
entry_point: docs/skills/ci-workflows.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [ci, workflows, actions, promotion]
description: >-
  Build and contract jobs, promotion, branch sync, parity and extension
  update workflows, reusable actions and Renovate pins. Use when editing
  GitHub Actions or debugging CI.
metadata:
  type: reference
---

# CI Workflows

This overview covers the build, promotion, branch-sync, parity-sync, and
post-build verification workflows, not the complete workflow inventory:

- `.github/workflows/build.yml` -- pull requests, merge queue groups
  (`merge_group`), pushes to `testing`, a manual dispatch. Top-level
  `permissions: {}`; each job grants its own. Cancels in-progress runs per
  workflow and ref for pull requests only. The independent `pinned_fetches`
  job runs the shared download-pinning gate in fail mode, including on merge
  groups and dispatches. Its documented substring exceptions live in
  `.github/pinned-fetches-allowlist.txt`; review them when their fetches change.
  A merge group also runs `contract`, but `kernel_cache`, `build_main` and
  `build_kernel` skip it because they push and sign (see `merge-queue.yml`
  below). A dispatch
  with `contract_only=true` skips the publication jobs and `dispatch-iso`.
  Publishing dispatches (`contract_only=false`) must target `testing`; a
  dispatch from any other ref, including the Actions UI default `main`,
  fails unless `contract_only=true`. An optional `sha` input must equal the
  dispatched ref's `github.sha`, so a sync race fails closed and `testing`
  waits for the next sync or nightly run.
  With the default `review_vm=false`, only the source checks run. Set
  `review_vm=true` as well to build a disposable image on the runner, compose
  and boot an offline ISO, and retain test evidence; `review_iso_all_flavors`
  expands that job from main flavors to every flavor in `config/flavors.json`.
  The review ISO defaults to zstd level 15 so all flavors fit for VM testing.
  Set `review_iso_compression_level=3` to measure the production ISO budget;
  `evidence/iso-size.json` records the image size even when the 8 GiB guard
  fails. This path has read-only registry access and never publishes or signs
  the review image. For kernel-cache consumers, first build the matching signed
  cache through the normal pipeline.
- `.github/workflows/merge-queue.yml` -- `merge_group` only. The `main`
  ruleset requires `contract` and `build_main / Check all testing builds
  successful`; the merge queue waits for both on its own commit. The second
  name is normally the aggregate of `build.yml`'s call to the pinned
  `projectbluefin/actions` reusable build, which pushes, signs, attests and
  opens issues for every event other than `pull_request`, so it must never
  run on a queue ref. This workflow reports the same name from a matrix of
  `just build-ghcr utah testing <flavor>` over `scripts/flavors.py list-main`,
  the local path `review_vm` uses, with `REGISTRY_CACHE_WRITE=0` and a token
  limited to `contents: read` and `packages: read`. It is a separate file
  because a skipped job of the same name in `build.yml` would report a second
  `build_main / Check all testing builds successful` on every pull request.
  Rechunking, SBOM and signatures are publication steps and stay on
  `testing`. `tests/test_merge_queue.py` holds the name, the trigger, the
  read-only permissions, and the absence of any push, sign, attest, issue or
  dispatch step.
- `.github/workflows/promote-testing-to-main.yml` -- pushes to `testing`, a
  nightly cron, and manual dispatch. Maintains the `testing` -> `main` squash
  PR; it never moves image tags and keeps `run_e2e: false` (the pinned reusable
  gate keys E2E status to the branch tip; see release-cadence).
- `.github/workflows/sync-main-to-testing.yml` -- source pushes to `main`,
  nightly cron, and manual dispatch; explicitly dispatches the testing build
  after syncing. Token-authenticated branch pushes alone do not start CI.
- `.github/workflows/update-gnome-extensions.yml` -- weekly and manual
  submodule updates. `prepare-gnome-extension-bumps.py` validates each
  tracked-branch head; missing or invalid metadata keeps that source pinned,
  warns in the run summary, and does not block other candidates. Extensions
  in `ARCHIVES` ship from a pinned, sha256-checked zip, so their gitlink is
  never bumped alone (#1290): move it by hand with the archive URL and digest.
  It uses the
  metadata paths in `scripts/verify-gnome-extensions.py`
  (`SOURCE_EXTENSIONS`); update that map when adding an extension. Paths are
  passed as argv and `shell-version` values are allow-listed in the report.
  Trusted `main` alone may mint a repository-scoped Mergeraptor App token,
  with contents and pull-request write permissions, after metadata
  preparation. The token creates or updates the proposal and is revoked
  afterward; it is never passed to extension code. App-authenticated PR
  events start the normal contract and image-build workflows on the exact
  proposed head, unlike the old `GITHUB_TOKEN` proposal followed by a
  contract-only dispatch (#1073). Do not dispatch a publishing build on the
  candidate branch or fabricate required-check statuses.
  Hive's authenticated review, required checks, holds and soak still govern
  landing; this producer does not arm native auto-merge or relax those gates.
  A successful image build is not proof of a reported rendering/CPU fix:
  retain #992/#997 until actual runtime evidence addresses them.
- `.github/workflows/image-baselines.yml` -- weekly and manual
  re-measurement of the Dakota and Utah image baselines (Dakota is the only
  parity target; see `baselines/README.md`). Like the
  extension updater, trusted `main` alone mints a repository-scoped Mergeraptor
  App token after `just baselines` has run, and uses it only to create or
  update the proposal, so the normal contract and image-build checks run on
  the proposed head. The measurement step keeps the read-only job token.
- `.github/workflows/post-testing-e2e.yml` -- successful non-PR testing builds
  explicitly dispatch this, or manually supply a successful testing build run ID.
  Its `promote-to-testing` job needs the LUKS, production ISO and testsuite
  jobs. Only when all pass does it record per-digest evidence and move
  `:testing` through a monotonic ratchet; a red testsuite leg blocks
  promotion, with no override (#1329). The promotion to `main` sets
  `run_e2e: false`; the release gate requires that evidence for the exact
  `:testing` digests and re-runs `smoke,common`.
  See [testsuite triage](ci-workflows/references/testsuite-triage.md) for
  known failures and [iso-e2e](ci-workflows/references/iso-e2e.md) for the
  ratchet.
- `.github/workflows/execute-release.yml` -- promotion pushes to `main`,
  manual dispatch and a daily 06:00 UTC check retag `:stable`. The daily run
  releases only when `:stable` last moved over seven days ago (or is missing)
  and `:testing` points at a different digest, and otherwise posts a
  `::notice` saying why not. `scripts/release-trigger.py` dates `:stable` by
  the retag step of past runs, never by image metadata
  ([release-cadence](ci-workflows/references/release-cadence.md)). Its
  `e2e-evidence` job gates every release on
  the E2E evidence recorded for the exact digests `:testing` points at, not on
  the `testing` branch tip, then hands the reusable release the
  commit-addressed source tags it verified.
- `.github/workflows/prow.yml` -- slash commands on issues and pull requests,
  roster authorization via `projectbluefin/.project/.github/workflows/prow-authorize.yml`,
  issue transfers via `prow-transfer.yml`, and label syncing via
  `cncf/prow-github-actions`. Labels and configuration are centrally defined in
  `projectbluefin/.project/prow.yaml`, and approvers are read from the root `OWNERS`
  file, kept in sync with `maintainers.yaml`. Run `label-sync` via manual
  `workflow_dispatch` on Actions → Prow after merge and after every change to
  `projectbluefin/.project/prow.yaml` to initialize or update repository labels.
  A separate Sunday 03:37 UTC cron also syncs labels. Merge reconciliation runs
  `sweep lgtm` every four hours at minute 37; other supported events run `lgtm`.
  The four-hour backstop trades scheduled runner use for up to four hours of
  reconciliation delay plus GitHub scheduling delays, not immediate CI-green
  handling. Existing review, check, and queue protections are unchanged.

CI delegates builds, vulnerability reporting, keyless signatures, provenance,
and caching to `projectbluefin/actions@v1` (originated as a `docs/building.md`
design bullet; now lives in this skill). The reusable workflow skips package
update-interval xattrs, rechunking, and SBOM steps on `testing` by default.
Both Utah image jobs explicitly set `rechunk: "true"`, opting the testing
stream into those steps (#226; actions#557). This does not settle published
chunkah layer order: `scripts/check-reproducible-build.sh` compares native
`RootFS.Layers`, not the post-rechunk image (#313; actions#591).

## Merge queue

Both required checks report on `merge_group`, so the ruleset's strict "branch
must be up to date" policy can be replaced by a merge queue: the queue tests
each pull request on top of `main` and the entries ahead of it instead of
making every open pull request rebase and rebuild after each merge.

Since 2026-11-18 `main` requires the merge queue (ruleset 22705160: squash,
groups of up to 5, 120-minute check timeout, merge only when every check
passes). Strict freshness is off since 2026-11-18, after the first queue group
the hive enqueued (#1335) passed: the queue tests each entry on top of `main`
and the entries ahead of it instead.

Automations that merge into `main`, and what each needs under a required
queue:

| Automation | How it merges | Under a required merge queue |
| --- | --- | --- |
| `prow.yml` (tide, `cncf/prow-github-actions` v3.5.0 run through `projectbluefin/.project`'s `prow-run.yml`; `tide.merge_queue: auto`, `merge_method: squash` in `projectbluefin/.project/prow.yaml`) | On `lgtm` + `approved`, enqueues with `enqueuePullRequest` on a mergeraptor App installation token, as `mergeraptor[bot]` | Works: App events start `merge_group` runs (chairlift#792 was queued this way on 2026-11-15, and its group checks started 18 s later). If the App token can't be created, `prow-run.yml` falls back to the job's `GITHUB_TOKEN`; a group enqueued that way gets no `merge_group` runs and times out, so a maintainer clicks "Merge when ready" |
| Renovate (`renovate.json`, `local>projectbluefin/renovate-config` = `config:recommended`) | No automerge; the `utah-packages` digest group sets `automerge: false` | A maintainer (or prow) enqueues |
| `update-gnome-extensions.yml`, `image-baselines.yml`, `promote-testing-to-main.yml` | Open or maintain pull requests only | A maintainer (or prow) enqueues |
| `sync-main-to-testing.yml` | Pushes `testing`, not `main` | Unaffected |

Prow's App token asks for contents, issues, pull requests, commit statuses and
Actions write: `statuses:write` for exact-head LGTM binding and `actions:write`
for `/test`, `/retest` and `/ok-to-test`. The org's mergeraptor installation
grants all five (`statuses:write` accepted 2026-11-14,
projectbluefin/.project#36); chairlift and common run the same caller.

## Unattended landing with strict freshness

Arm GitHub's **native auto-merge** on a PR to land it unattended: Hive, a
Renovate rule with platform automerge enabled, or a maintainer can do this.
Merely opening a PR does not arm it. Required checks, reviews and the merge
queue still apply; this automation does not bypass or relax them.

`.github/workflows/update-armed-prs.yml` runs on every push to `main`, manual
dispatch on `main`, and at minutes 17 and 47 each hour. It paginates open PRs
targeting `main` and requests a **merge update**, never a rebase, only when
native auto-merge is armed and `mergeStateStatus` is `BEHIND`. It skips drafts,
`hold`, `do-not-merge` and `do-not-merge/*` labels, conflicted (`DIRTY`) PRs
and other merge states. The fallback schedule catches auto-merge armed after
the last push and mergeability still `UNKNOWN` when a push run inspected it.
A newer run cancels its predecessor in a repository-wide concurrency group.

Updates use a mergeraptor App installation token, not `GITHUB_TOKEN`: App
updates trigger PR CI, whereas `GITHUB_TOKEN` updates suppress that event.
The organization must expose `MERGERAPTOR_APP_ID` and
`MERGERAPTOR_PRIVATE_KEY` to this repository and install the App with contents
and pull-request write access. The workflow requests only those permissions,
explicitly scopes the token to this repository (no organization-wide
`owner`), leaves the job's `GITHUB_TOKEN` permissions empty, and lets the
pinned token action revoke the token afterward. Only trusted main triggers
can mint it; there is **no checkout or execution of PR code**, and PR metadata
is handled as data by inline code, not interpolated into a shell command.

The update API receives the observed head SHA, so an intervening author push
rejects the stale request. One PR's update failure, including a fork that
disallows maintainer edits, is recorded in the job summary without blocking
other PRs. Discovery/authentication failure still fails the job. Conflicts,
fork access and persistent update failures need the author or Hive to resolve;
this job neither resolves conflicts nor arms auto-merge or merges a PR.
Strict freshness stays on. Behavioral coverage lives in
`tests/test_update_armed_prs.py`, which executes the workflow body against a
fake API client without using live credentials.

## Detailed workflow references
- [Package snapshot proof](ci-workflows/references/package-snapshot.md)
- [Contract gate](ci-workflows/references/contract-gate.md)
- [Kernel cache and build matrix](ci-workflows/references/kernel-and-build.md)
- [ISO end-to-end verification](ci-workflows/references/iso-e2e.md)
- [Release cadence and factory pin](ci-workflows/references/release-cadence.md)
- [Testsuite triage runbook](ci-workflows/references/testsuite-triage.md)

## Verification

Python packages installed by CI must use exact `==` versions, directly or
through a pinned requirements file. `just check` enforces this for workflow
run steps and Justfile recipes with `tests/test_python_install_pins.py`; see
[the contract gate](ci-workflows/references/contract-gate.md#python-install-pins)
for supported command forms and requirements-file rules.

```bash
just check
~/.local/bin/pre-commit run actionlint --all-files
```

Front-matter presence checks feed `grep -q` with a here-string. With
`pipefail`, an early reader exit can make a successful matching pipeline
look unsuccessful when its writer receives SIGPIPE.
