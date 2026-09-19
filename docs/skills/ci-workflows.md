---
name: ci-workflows
version: "1.1"
last_updated: "2026-09-18"
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
  build.yml contract gate, Dakota PR and merge-queue build topology,
  kernel-cache job, matrix split, promote and sync, actions@v1 delegation.
  Use when changing .github/workflows/ or debugging a red run.
metadata:
  type: reference
---

# CI Workflows

Workflows are thin callers into `projectbluefin/actions@v1` reusables, each
pinned to a SHA tagged `v1`:

- `.github/workflows/build.yml` -- pull requests, merge queue (`merge_group`),
  pushes to `testing`, and manual dispatch. Top-level `permissions: {}`; each
  job grants its own. Cancels in-progress runs for pull requests only
  (`cancel-in-progress: ${{ github.event_name == 'pull_request' }}`), ensuring
  merge queue, scheduled, and release builds queue safely without dropping work.
- `.github/workflows/promote-testing-to-main.yml` -- pushes to `testing`,
  nightly cron, manual dispatch. Serialized via `utah-promote-testing`.
- `.github/workflows/sync-main-to-testing.yml` -- pushes to `main`, nightly
  cron, manual dispatch; dispatches testing build. Serialized via `utah-sync-main-to-testing`.
- `.github/workflows/post-testing-e2e.yml` -- testing build verification.
  Serialized via `utah-iso-e2e`.
- `.github/workflows/execute-release.yml` -- promotion and release dispatch.
  Serialized via `utah-execute-release`.

CI delegates builds, vulnerability reporting, SBOMs, keyless signatures,
provenance, caching, and rechunking to `projectbluefin/actions@v1` (originated
as a `docs/building.md` design bullet; now lives in this skill).

## PR validation and merge-queue build topology

In accordance with Dakota's CI topology:

1. **PR validation protects core image builds without kernel compile overhead:**
   PRs run the cheap `contract` job (`just check`, `just check-parity`, `just check-repos`, and
   flavor resolution) and primary `build_main` image build, validating container
   composition and package transactions without waiting for the 45-minute kernel cache compile.
   PRs skip the secondary-flavor kernel builds (`kernel_cache`, `build_kernel`).
2. **Merge queue performs full release-candidate builds:** When a PR enters the
   merge queue, GitHub fires `merge_group`. Merge queue builds execute the full
   matrix including secondary flavors: `contract`, `build_main`, `kernel_cache`, and `build_kernel`.
3. **Release-oriented concurrency:** `cancel-in-progress` is active only for
   pull requests (`${{ github.event_name == 'pull_request' }}`). Merge queue,
   testing pushes, scheduled runs, and promotion workflows queue safely instead
   of being superseded and discarded mid-run.
4. **Explicit timeouts and log preservation:** Every standalone runner job defines an explicit
   `timeout-minutes` (reusable workflows inherit timeout configuration from the reusable workflow).
   Long-running tasks capture build output and upload logs as artifacts on completion or failure
   (e.g. `kernel-cache-build-logs`).
5. **Transient push retries and explicit secondary-flavor policy:** Registry
   pushes (`podman push` for kernel cache and `skopeo copy` for testing tag
   promotion) retry transient failures up to three times with backoff. Secondary
   flavors (`nvidia`, `gaming`, `nvidia-gaming`) build in `build_kernel` on top
   of the kernel cache; their configuration is owned strictly by `config/flavors.json`.

## contract: the cheap gate

`build.yml` opens with a container-based gate so a contract package that none of
Utah's repositories provide fails in seconds instead of surfacing as an
opaque `exit status 71` from the image build (comment,
`.github/workflows/build.yml`). It runs three checks:

- `just check` -- manifest validation, including the workflow output check
  (`scripts/check_workflow_outputs.py`), the download integrity guard
  (`scripts/check-download-integrity.py`), the syntax gate
  (`scripts/check-script-syntax.py`), host-side unit tests (`just test`),
  and the ban on flavor literals in workflows.
- `just check-parity` -- `packages/bluefin.toml` against Bluefin's upstream.
- `just check-repos` -- the complete installation transaction against the
  digest-pinned base and package repository, including extension build tools.

### CI guard scripts and test coverage

The fast gate relies on pure-verdict Python scripts under `scripts/` to halt
the build before expensive compilation or container builds run:

- `scripts/check-download-integrity.py`: enforces that composition recipes do
  not resolve mutable `releases/latest` URLs, and that any executable download
  (`.run`, `.tar.gz`, `.tgz`, `.rpm`, `.flatpak`, `.service`, `.timer`) via `curl`
  or `wget` is verified against a digest (`sha256sum`, `sha512sum`, `--check`) or
  signature (`cosign`, `gpg --verify`). Clearance is per-file. Flathub descriptor
  downloads (`flathub.flatpakrepo`, `appstream`) and comment lines are exempt.
  Exercised by black-box tests in `tests/test_check_download_integrity.py`.
- `scripts/check_workflow_outputs.py`: parses workflows under `.github/workflows/`
  and ensures that every job output referencing `steps.<id>.outputs` points to a
  step id defined in that same job. Step ids never leak across jobs, non-step
  expressions (`inputs.*`, `github.*`, `env.*`) are accepted, and all dangling
  references across all workflow files are reported.
  Exercised by black-box tests in `tests/test_check_workflow_outputs.py`.

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

Source pushes to `main` and the nightly schedule call
`reusable-sync-branches.yml@v1`, then explicitly dispatch `build.yml` on
`testing` with `actions: write`. README/verification-only pushes are excluded
to avoid evidence-update build loops. Nightly runs still sync those changes.

## ISO LUKS gate and screenshots

`post-testing-e2e.yml` downloads the originating build's digest artifacts.
The final `dispatch-iso` build job invokes it with `workflow_dispatch`, not
`workflow_run`: the latter did not fire after our GITHUB_TOKEN-dispatched
build. Explicit dispatch is a documented exception to token recursion
prevention. The resolver waits up to five minutes for the dispatching build
to finish and still requires a successful conclusion before reading artifacts.
`scripts/resolve-e2e-inputs.py` rejects PRs, foreign repositories, failed runs,
wrong branches/workflows, mutable references, conflicting digests, and missing
flavors. The expected set comes from `scripts/flavors.py images`.

Each configured image is pulled by digest, composed into a disposable debug
ISO, and passed to the existing `iso/scripts/luks-e2e.sh`. Both guests have
restricted networking. CI requires KVM, PNG screenshots, and OCR evidence
that fastfetch ran in the graphical terminal. Test credentials are confined
to the disposable ISO/disk; neither is uploaded or released.

Every matrix job preserves build/test logs, serial logs, and screenshots,
including on failure. Only passing jobs upload `docs/verification` with the
source commit, original build run, E2E run, image digest and ISO checksum.
The promotion job depends on the entire matrix; a superseded testing commit
cannot move tags. Registry tag copies are sequential, not an atomic multi-tag
transaction: a registry failure can interrupt promotion after a partial copy.

A separate least-privilege job proposes the main desktop's screenshots in
`automation/iso-verification`, a documentation PR. It updates only the README
evidence block and `docs/verification/`, preserving the current README's other
content. The repository must allow Actions to create pull requests; a denied
write fails this job visibly, without deleting test artifacts. It does not
auto-merge the evidence PR or imply a fresh pass for a different commit.

For a deliberate rerun, dispatch Post-Testing E2E with `build_run_id` from a
successful testing build containing the current harness. Do not pass a PR
build: PRs do not publish immutable images. This matrix validates emulated
UEFI desktop installation, not Secure Boot, TPM unlock, or physical GPUs.

The ISO size is bounded in `iso/scripts/build-iso.sh`: it fails closed above
`ISO_MAX_GB` (override per-run with `UTAH_ISO_MAX_GB`) once the ISO is written,
so a drift like the 7.7G -> 8.6G jump in #128 fails the job instead of landing
silently. The guard lives in the build script, so it holds for every caller
(local `just iso`, the CI LUKS job, and any deliberate rerun), not just one
workflow.

## Verification

```bash
just check
~/.local/bin/pre-commit run actionlint --all-files
```
