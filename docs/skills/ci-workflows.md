---
name: ci-workflows
version: "1.0"
last_updated: "2026-09-05"
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
  build.yml contract gate, kernel-cache job, main/kernel matrix split,
  promote-testing-to-main and sync-main-to-testing, actions@v1 delegation.
  Use when changing .github/workflows/ or debugging a red run.
metadata:
  type: reference
---

# CI Workflows

Three workflows, all thin callers into `projectbluefin/actions@v1` reusables,
each pinned to a SHA tagged `v1`:

- `.github/workflows/build.yml` -- pull requests, pushes to `testing`, a
  nightly cron, and manual dispatch. Top-level `permissions: {}`; each job
  grants its own. Cancels in-progress runs per workflow and ref.
- `.github/workflows/promote-testing-to-main.yml` -- pushes to `testing`, a
  nightly cron, and manual dispatch.
- `.github/workflows/sync-main-to-testing.yml` -- every push to `main`.

CI delegates builds, vulnerability reporting, SBOMs, keyless signatures,
provenance, caching, and rechunking to `projectbluefin/actions@v1` (originated
as a `docs/building.md` design bullet; now lives in this skill).

## contract: the cheap gate

`build.yml` opens with a network-only gate so a contract package that none of
Utah's repositories provide fails in seconds instead of surfacing as an
opaque `exit status 71` from the image build (comment,
`.github/workflows/build.yml`). It runs three checks:

- `just check` -- manifest validation, including the workflow output check
  (`scripts/check_workflow_outputs.py`) and the ban on flavor literals in
  workflows.
- `just check-parity` -- `packages/bluefin.toml` against Bluefin's upstream.
- `just check-repos` -- contract package names against the enabled
  repositories.

The same job resolves the flavor set and splits it in two by what each
flavor builds on -- `main` on the pristine Hummingbird base, the rest on the
kernel cache image -- emitting `main_flavors`, `kernel_flavors`, and
`needs_kernel` (step "Resolve the image flavors",
`.github/workflows/build.yml`). The split rationale and the flavors.py
commands live in [flavors.md](flavors.md). An empty resolved set fails
loudly there, because an invalid matrix creates no image job at all and the
only symptom is `build_container: failure` from the aggregator (comment,
`.github/workflows/build.yml`).

## kernel_cache: skipped unless needed, skipped when published

The kernel cache job runs only when `needs_kernel` is `true` -- while the
matrix is main-only, building it is 45 minutes spent on an image nothing
consumes (comment, `.github/workflows/build.yml`). When it does run, it
frees runner disk, logs in to GHCR with `GITHUB_TOKEN`, and probes the
content-hash tag with `podman pull`: a tag that is already published is a
cache hit and the job exits without building; only a miss builds
`Containerfile.kernel` and pushes (step "Build the kernel cache image if it
is not published yet", `.github/workflows/build.yml`). What the tag hashes
and why lives in [kernel-cache.md](kernel-cache.md).

## The build matrix calls reusable-build.yml twice

`build_main` needs only `contract`, so `main` starts the moment the gate
passes; `build_kernel` needs `contract` and `kernel_cache`, so a cache miss
holds up only the flavors that consume it. Both call
`reusable-build.yml@eb4c546389f19ad9e42f1af052623de73c620ebb # v1`, and
`just check` asserts that pin with
`grep -qE 'reusable-build\.yml@(v1|[0-9a-f]{40} # v1)$' .github/workflows/build.yml`
(recipe, `Justfile`, `check`). Both pass `publish_stream_tag: "false"` --
testing is advanced only after post-testing-e2e validates the build
(comment, `.github/workflows/build.yml`).

The two calls carry different `brand_name` values on purpose. The reusable
workflow keys its own cancel-in-progress concurrency group on `brand_name`
and `stream_name`, so two calls carrying the same pair would cancel each
other. Utah's Justfile ignores `base_name` when naming images -- image names
come from the flavor alone -- so the only visible effect is the name of the
digest artifact, which post-testing-e2e matches by glob (comment,
`.github/workflows/build.yml`).

## Registry caches are read-only on pull requests

The kernel cache image and the layer cache are both published private by
default, and the reusable build workflow only logs in to GHCR for non-PR
events -- so pulling either would 401 on exactly the runs that need them
most. It passes `GITHUB_TOKEN` through to the recipe, and `build-ghcr` uses
it to log in (comment, `Justfile`, `build-ghcr`).

`build-ghcr` passes `--cache-from` for the image's own GHCR package always,
and `--cache-to` only when `REGISTRY_CACHE_WRITE=1`, which the reusable
workflow sets for non-PR events only. Pull-request and local builds read the
cache and never write it, so nothing a PR does can poison what testing
builds from (comment, `Justfile`, `build-ghcr`). The full registry layer
cache narrative lives in [containerfile.md](containerfile.md).

## promote-testing-to-main

A `variants` job reads the same `scripts/flavors.py images` list the build
matrix does -- promoting an image the build no longer produces fails late
and confusingly, so this reads the same list (comment,
`.github/workflows/promote-testing-to-main.yml`). The `promote` job calls
`reusable-promote-squash.yml@v1` (same SHA pin) against
`ghcr.io/<owner>`, with a cosign identity regexp pinned to
`build.yml@refs/heads/testing` and `run_e2e: false`.

## sync-main-to-testing

Every push to `main` calls `reusable-sync-branches.yml@v1` (same SHA pin)
with `contents: write` and nothing else -- there is no local logic to drift
(workflow, `.github/workflows/sync-main-to-testing.yml`).

## Verification

```bash
just check
~/.local/bin/pre-commit run actionlint --all-files
```
