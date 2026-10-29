---
name: ci-workflows
version: "1.2"
last_updated: "2026-10-25"
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

- `.github/workflows/build.yml` -- pull requests, pushes to `testing`, a
  manual dispatch. Top-level `permissions: {}`; each job
  grants its own. Cancels in-progress runs per workflow and ref. A dispatch
  with `contract_only=true` skips the publication jobs and `dispatch-iso`.
  Publishing dispatches (`contract_only=false`) must target `testing`; a
  dispatch from any other ref, including the Actions UI default `main`,
  fails unless `contract_only=true`. An optional `sha` input must equal the
  dispatched ref's `github.sha`, so a sync race fails closed and `testing`
  waits for the next sync or nightly run.
  With the default `review_vm=false`, only the contract checks run. Set
  `review_vm=true` as well to build a disposable image on the runner, compose
  and boot an offline ISO, and retain test evidence; `review_iso_all_flavors`
  expands that job from main flavors to every flavor in `config/flavors.json`.
  The review ISO defaults to zstd level 15 so all flavors fit for VM testing.
  Set `review_iso_compression_level=3` to measure the production ISO budget;
  `evidence/iso-size.json` records the image size even when the 8 GiB guard
  fails. This path has read-only registry access and never publishes or signs
  the review image. For kernel-cache consumers, first build the matching signed
  cache through the normal pipeline.
- `.github/workflows/promote-testing-to-main.yml` -- pushes to `testing`, a
  nightly cron, and manual dispatch. Maintains the `testing` -> `main` squash
  PR; it never moves image tags and keeps `run_e2e: false` (the pinned reusable
  gate keys E2E status to the branch tip; see release-cadence).
- `.github/workflows/sync-main-to-testing.yml` -- source pushes to `main`,
  nightly cron, and manual dispatch; explicitly dispatches the testing build
  after syncing. Token-authenticated branch pushes alone do not start CI.
- `.github/workflows/update-bluefin-parity.yml` -- nightly and manual
  dispatch. It resolves Bluefin `main` and uses one fixed branch,
  `automation/bluefin-parity`, so `create-pull-request` updates the existing
  review rather than opening duplicates. It does not auto-merge. That branch is
  disposable: `create-pull-request` rebuilds it from `main` plus the generated
  changes each run and force-resets it when the result differs, so a commit
  pushed onto the open bump PR is discarded at the next run. Overlay fixes the
  bump needs go in their own pull request against `main`, not onto the bump
  branch; see `package-contract.md`. Before
  proposing, it reruns `scripts/generate-site-data.py` and
  `scripts/check-doc-counts.py --write`, so the bump carries the new
  `site/data/packages.json` and the README / `package-contract.md` counts
  that `just check` compares against the manifests. The generator is passed
  `--generated-at` with the upstream commit date rather than defaulting to
  today, so an unchanged upstream ref regenerates byte-identical files and
  `create-pull-request` leaves the open bump branch alone.
  The proposal action runs even when upstream equals `main`: it must see the
  empty diff to close a previously opened bump after an upstream reversion.
  Its body file is created on both paths, while explicit CI dispatch remains
  restricted to `created`/`updated` proposals.

  The bump PR would otherwise arrive with **no checks**: GitHub does not
  start `on: pull_request` workflows for pull requests created with the
  default `GITHUB_TOKEN`. The workflow's last step dispatches
  `gh workflow run build.yml --ref automation/bluefin-parity -f
  contract_only=true` right after `create-pull-request` runs, so the run
  attaches to the branch head, which is the PR head, and
  `check`/`check-parity`/`check-repos` report on the bump PR itself. It is
  contract-only on purpose: a dispatch is not a `pull_request` event, so a
  full build would push and sign `testing` images from an upstream package
  set no maintainer has reviewed yet. The images are built when the merged
  change reaches `testing`. Needs `actions: write`, which the job holds.
  Gated on `create-pull-request`'s own `pull-request-operation` output being
  `created` or `updated` (not on the parity diff alone), so a nightly run
  against an unmerged, unchanged bump branch does not re-dispatch for
  nothing.
- `.github/workflows/update-gnome-extensions.yml` -- weekly and manual
  submodule updates. `prepare-gnome-extension-bumps.py` validates each
  tracked-branch head; missing or invalid metadata keeps that source pinned,
  warns in the run summary, and does not block other candidates. It uses the
  metadata paths in `scripts/verify-gnome-extensions.py`
  (`SOURCE_EXTENSIONS`); update that map when adding an extension. Paths are
  passed as argv and `shell-version` values are allow-listed in the report.
  The dispatch uses `contract_only=true`: source contract checks only, not an
  image build or the image-side extension verifier. A trusted full image build
  remains necessary before merge. No automated merge is enabled.
- `.github/workflows/post-testing-e2e.yml` -- successful non-PR testing builds
  explicitly dispatch this, or manually supply a successful testing build run ID.
  Its `promote-to-testing` job records per-digest E2E evidence as commit
  statuses and moves `:testing` through a monotonic ratchet: `:testing` is the
  latest E2E-green commit, not the branch tip, so merges to `main` during a
  run never block promotion and an older run never moves it backwards
  (`scripts/promotion-ratchet.py`; details in
  [iso-e2e](ci-workflows/references/iso-e2e.md)).
- `.github/workflows/execute-release.yml` -- promotion pushes to `main` and
  manual dispatch retag `:stable`. Its `e2e-evidence` job gates the release on
  the E2E evidence recorded for the exact digests `:testing` points at, not on
  the `testing` branch tip, then hands the reusable release the
  commit-addressed source tags it verified
  ([release-cadence](ci-workflows/references/release-cadence.md)).
- `.github/workflows/prow.yml` -- slash commands on issues and pull requests,
  roster authorization via `projectbluefin/.project/.github/workflows/prow-authorize.yml`,
  issue transfers via `prow-transfer.yml`, and label syncing via
  `cncf/prow-github-actions`. Labels and configuration are centrally defined in
  `projectbluefin/.project/prow.yaml`, and approvers are read from the root `OWNERS`
  file, kept in sync with `maintainers.yaml`. Run `label-sync` via manual
  `workflow_dispatch` on Actions → Prow after merge and after every change to
  `projectbluefin/.project/prow.yaml` to initialize or update repository labels.

CI delegates builds, vulnerability reporting, keyless signatures, provenance,
and caching to `projectbluefin/actions@v1` (originated as a `docs/building.md`
design bullet; now lives in this skill). The reusable workflow skips package
update-interval xattrs, rechunking, and SBOM steps on `testing` by default.
Both Utah image jobs explicitly set `rechunk: "true"`, opting the testing
stream into those steps (#226; actions#557). This does not settle published
chunkah layer order: `scripts/check-reproducible-build.sh` compares native
`RootFS.Layers`, not the post-rechunk image (#313; actions#591).

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
