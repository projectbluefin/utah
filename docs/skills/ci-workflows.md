---
name: ci-workflows
version: "1.0"
last_updated: "2026-09-29"
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
  promote-testing-to-main and sync-main-to-testing, actions@v1 delegation,
  weekly factory-pin bump. Use when changing .github/workflows/ or debugging a
  red run.
metadata:
  type: reference
---

# CI Workflows

Three workflows, all thin callers into `projectbluefin/actions@v1` reusables,
each pinned to a SHA tagged `v1`:

- `.github/workflows/build.yml` -- pull requests, pushes to `testing`, a
  manual dispatch. Top-level `permissions: {}`; each job
  grants its own. Cancels in-progress runs per workflow and ref.
- `.github/workflows/promote-testing-to-main.yml` -- pushes to `testing`, a
  nightly cron, and manual dispatch.
- `.github/workflows/sync-main-to-testing.yml` -- source pushes to `main`,
  nightly cron, and manual dispatch; explicitly dispatches the testing build
  after syncing. Token-authenticated branch pushes alone do not start CI.
- `.github/workflows/post-testing-e2e.yml` -- successful non-PR testing builds
  explicitly dispatch this, or manually supply a successful testing build run ID.

CI delegates builds, vulnerability reporting, keyless signatures, provenance,
and caching to `projectbluefin/actions@v1` (originated as a `docs/building.md`
design bullet; now lives in this skill). The reusable workflow supports
rechunking and build SBOMs, but its `testing`-stream guard skips the package
update-interval xattrs, rechunk, and SBOM steps. Utah publishes only the
`testing` stream, so its images are not rechunked and do not produce those SBOM
artifacts; enabling them requires a reusable-workflow change (#131).

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
- `just check-parity` -- `packages/bluefin.toml` against Bluefin's upstream
  pinned at `packages/.bluefin-parity-ref`.
- `just check-repos` -- the complete installation transaction against the
  digest-pinned base and package repository, including extension build tools.

### CI guard scripts and test coverage

The fast gate relies on pure-verdict Python scripts under `scripts/` to halt
the build before expensive compilation or container builds run:

- `scripts/check-download-integrity.py`: enforces that composition recipes do
  not resolve mutable `releases/latest` URLs, and that any executable download
  (`.run`, `.tar.gz`, `.tgz`, `.rpm`, `.flatpak`, `.service`, `.timer`) via `curl`
  or `wget` is verified against a digest (`sha256sum`, `sha512sum`) or
  signature (`cosign`, `gpg --verify`). Clearance is scoped to the fetch: the
  verifier must sit in the fetch's own `&&` chain (a backslash-continuation
  run is one command) or within the ten lines that follow it, must be code
  rather than comment text, and a bare `--check` never clears on its own --
  `sha256sum --check` clears through `sha256sum`. Flathub descriptor
  downloads (`flathub.flatpakrepo`, `appstream`) and comment lines are exempt
  from the extension heuristic only; `scripts/configure-services.sh` still
  pins `flathub.flatpakrepo` by sha256, because the descriptor carries the
  `Url=` and `GPGKey=` every Flatpak on the image is verified against.
  Verify the trust behavior by running `configure-services.sh` in a disposable
  image: a matching descriptor must install unchanged, while a hash mismatch
  must exit nonzero without replacing an existing remote. Source ordering or
  string assertions do not prove that rejection path.
  `tests/test_flathub_descriptor.py` runs the whole script against scratch
  filesystem roots and a committed descriptor fixture, with real hashing and
  installation. It covers matching bytes and rejection without remote replacement.
  Scanning is per *logical* line: backslash continuations are joined before
  matching, so a `curl` whose URL sits on a continuation line is still inspected
  and is reported at the line the command starts on. Matching raw lines instead
  made every multi-line download invisible to the gate. Comment lines never
  start or end a run: a `#` line with a trailing backslash does not swallow the
  command below it, and a `#` line inside a `RUN` continuation is dropped the
  way the Dockerfile parser drops it, so the run keeps going.
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

## kernel_cache: skipped unless needed, reused only when signed

The kernel cache job runs only when `needs_kernel` is `true` -- while the
matrix is main-only, building it is 45 minutes spent on an image nothing
consumes (comment, `.github/workflows/build.yml`). When it does run, it
frees runner disk, logs in to GHCR with `GITHUB_TOKEN`, and probes the
content-hash tag with `podman pull`. A published tag is a cache hit only if
its digest verifies; unsigned or wrongly signed hits are rebuilt from the
checkout, pushed, signed and verified just like misses. What the tag hashes
and why lives in [kernel-cache.md](kernel-cache.md).

A published tag is adopted only when its signature verifies; a miss is
pushed, signed, and verified before the job calls itself done. Two
boundaries decide whether that can work (job `env:` and the same step):

- podman and cosign must share one credential store. `podman login` by
  default writes `${XDG_RUNTIME_DIR}/containers/auth.json`, which cosign
  never reads -- it uses the Docker keychain at
  `${DOCKER_CONFIG}/config.json`. The job points both `DOCKER_CONFIG` and
  `REGISTRY_AUTH_FILE` at one `${RUNNER_TEMP}/utah-registry/config.json`,
  outside the image build context, and removes it in an always-run step.
  The image push and signature upload then authenticate with the same token;
  without it the push succeeds and `cosign sign` fails UNAUTHORIZED (#316).
- the signed digest must be the one the registry stored. After a push,
  `podman image inspect ... RepoDigests` reports the *local* manifest
  digest, which differs from the registry's, so the job signs the digest
  `podman push --digestfile` reports instead. The cache-hit path needs no
  such care: there the inspect runs after `podman pull`, which records the
  registry digest.

`cosign verify` pins the issuer and the identity to this exact workflow in
this repo, then accepts any `refs/heads/*` or `refs/pull/N/merge` ref. A
manual `workflow_dispatch` signs with `refs/heads/<branch>` -- and the
Actions UI defaults to the default branch -- so a regexp naming only
`testing` made a dispatched run sign the image and then fail its own
verify. Everyone who can dispatch this workflow can already sign from an
arbitrary branch via a same-repo pull request, so accepting branch heads
widens nothing (#316).

The consumer (`build-ghcr`) also fails closed: CI downloads cosign v2.6.1
with the platform SHA-256 pinned by `sigstore/cosign-installer@7e8b541e…`,
outside the context under `${RUNNER_TEMP}/utah-tools`, and invokes its absolute
path because the reusable builder uses `sudo` with `secure_path`. Local kernel
builds require cosign on PATH. The tag is resolved once through `skopeo`, the
same issuer/ref regexp verifies the resulting immutable digest, and only that
digest reaches `BASE_IMAGE`. Registry credentials use a private temporary
Docker keychain outside the checkout and are removed at recipe exit.

## The build matrix calls reusable-build.yml twice

`build_main` needs only `contract`, so `main` starts the moment the gate
passes; `build_kernel` needs `contract` and `kernel_cache`, so a cache miss
holds up only the flavors that consume it. Both call
`reusable-build.yml@4f6c41ff0a16a224f5e54ae80d7affbe2409b3d0 # v1`, and
`just check` asserts that pin with
`grep -qE 'reusable-build\.yml@(v1|[0-9a-f]{40} # v1)$' .github/workflows/build.yml`
(recipe, `Justfile`, `check`). Both pass `publish_stream_tag: "false"` --
testing is advanced only after post-testing-e2e validates the build
(comment, `.github/workflows/build.yml`). Both set `rechunk: "true"` to
opt the testing stream into rechunking and build SBOMs, which
reusable-build skips by default. That opt-in requires the reusable
workflow's `rechunk` input added in projectbluefin/actions#557 (which
4f6c41ff0a16a224f5e54ae80d7affbe2409b3d0 includes); `workflow_call` validates
the caller's `with:` against the declared inputs.

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
that fastfetch ran in the graphical terminal. The live-boot gate observes the
`UTAH_LIVE_READY` ready marker on the serial console in addition to
`graphical.target`: the marker is written only by the `utah-live-ready.service`
oneshot that runs after `display-manager.service`, so it catches a boot that
stopped just short of a usable display. Test credentials are confined to the
disposable ISO/disk; neither is uploaded or released.

Only after the entire LUKS matrix succeeds, `production-iso` composes a fresh
`DEBUG=0` x86_64 UEFI ISO from each same digest, writes its SHA-256 checksum,
and retains both as a 30-day Actions artifact. It is deliberately an artifact,
not a release: destination, flavor policy, naming, signing, and Secure Boot
are product decisions tracked by #186. Production-ISO composition is a
promotion prerequisite, so a failed production build cannot advance testing
tags. Debug ISOs and guest disks are never uploaded because they contain test
credentials.

**"Production" names the `DEBUG=0` build, not shippable media.** `DEBUG=0`
does exclude the test credentials and sshd path (`iso/live/src/configure-live.sh`),
so the retained artifact carries no secrets — but `iso/scripts/build-iso.sh`
hard-codes `enforcing=0 console=ttyS0,115200n8` on the boot entry for every
`DEBUG` value (the documented Issue #22 exception: rootless `podman unshare`
cannot write `security.selinux` xattrs into the squashfs root), so this ISO
boots SELinux-permissive with a serial console.
That was unremarkable while ISOs were disposable; retaining them for
30 days under the name "production" makes it worth stating plainly. Running
the live environment permissive is a product decision, and it belongs to #186
along with signing and Secure Boot — it must be settled there before any of
these ISOs reach users. Do not treat a green `production-iso` job as evidence
that the media is release-ready.

Each retained ISO is multi-GB (the size budget in `iso/scripts/build-iso.sh`
is the ceiling, not the measured size), per flavor, per dispatch, at 30-day
retention. That is real Actions storage; if the matrix widens, revisit the
retention window before the flavor count.

Every matrix job preserves build/test logs, serial logs, and screenshots,
including on failure. Only passing jobs upload `docs/verification` with the
source commit, original build run, E2E run, image digest and ISO checksum.
The promotion job depends on the LUKS and production-ISO matrices; a
superseded testing commit cannot move tags. Registry tag copies are sequential,
not an atomic multi-tag transaction: a registry failure can interrupt promotion
after a partial copy.

A separate least-privilege job proposes the main desktop's screenshots in
`automation/iso-verification`, a documentation PR. It also depends on
`production-iso`, so a failed production-ISO composition blocks this
screenshot-refresh PR too, not just testing-tag promotion — a LUKS-only
concern in `docs/verification` still has to wait on the whole matrix
composing cleanly. It updates only the README evidence block and
`docs/verification/`, preserving the current README's other content. The
repository must allow Actions to create pull requests; a denied write fails
this job visibly, without deleting test artifacts. It does not auto-merge
the evidence PR or imply a fresh pass for a different commit.

For a deliberate rerun, dispatch Post-Testing E2E with `build_run_id` from a
successful testing build containing the current harness. Do not pass a PR
build: PRs do not publish immutable images. This matrix validates emulated
UEFI desktop installation, not Secure Boot, TPM unlock, or physical GPUs.

The ISO size is bounded in `iso/scripts/build-iso.sh`: it fails closed above
the 6 GB `ISO_MAX_GB` default (override per-run with `UTAH_ISO_MAX_GB`) once
the ISO is written. Successful post-fix E2E run `35469913325` measured 3.9G
(utah), 4.6G (gaming), 5.2G (nvidia), and 5.3G (nvidia-gaming), leaving 0.7G
headroom for the largest flavor. The guard lives in the build script, so it
holds for every caller (local `just iso`, the CI LUKS job, and any deliberate
rerun), not just one workflow.

## Release and branch cadence, and the factory pin

The cadence is RFC'd in #336. What runs today:

- Open pull requests against `main`, never `testing`. `sync-main-to-testing.yml`
  resets `testing` to `main` on every push to `main` and again nightly on its
  own `20 22 * * *` schedule, so a commit merged straight into `testing` is
  orphaned: #404 was lost this way and had to be re-landed. `build.yml` runs on
  every pull request and declares `push: branches: [testing]`, but that trigger
  is not how a `main` commit reaches the image tags: the sync pushes `testing`
  with the workflow's own `GITHUB_TOKEN`, and a `GITHUB_TOKEN` push starts no
  workflow. `sync-main-to-testing.yml`'s `build` job therefore dispatches the
  build explicitly (`gh workflow run build.yml --ref testing`) once the sync
  job returns, which is the path that actually produces the images.
- `:testing` advances per green build, not on a clock: the tags move in
  `post-testing-e2e.yml`, after the LUKS ISO matrix and the production-ISO
  composition both pass. `promote-testing-to-main.yml` is the daily 04:00 UTC
  heartbeat, so `:testing` is at most a day behind `main` and `main` is at
  most a day behind the newest validated `testing` image.
- `:stable` moves in `execute-release.yml` on every promotion push to `main`,
  gated by `run_release_gate: true` over `smoke,common`. A weekly promotion
  rather than a per-promotion one is still an open question in #336 -- that is
  a maintainer policy call, not a code gap, and nothing in this tree should
  encode a guess at it.

`bump-factory-pin` is the part of #336 that is code. `ARG PACKAGE_IMAGE_SHA`
is the digest of `ghcr.io/projectbluefin/utah-packages`, the RPM repository
every image installs from, and until this tool existed nothing revved it: the
factory published GNOME 51 finals and the pin kept serving the previous digest
until a human noticed. `scripts/bump-factory-pin.py` resolves the tag through
the registry's own manifest endpoint, reading `Docker-Content-Digest` off the
response -- anonymous bearer token, no skopeo install, no credential in the
log -- and rewrites one line of the Containerfile.

The rewrite is deliberately timid, and `tests/test_bump_factory_pin.py` pins
why: it refuses a pin it could not parse, refuses a `PACKAGE_IMAGE_REF` that
does not compose from `PACKAGE_IMAGE`/`PACKAGE_IMAGE_SHA` (a bump that would
never reach the build), and replaces exactly one line. Its three modes are
`--print` (resolve and report, write nothing), `--check` (exit non-zero when
the pin is stale, write nothing), and the default (rewrite), plus `--digest` to
take a digest resolved by another job. The suite is offline by default; the one
test that talks to the registry runs only with `UTAH_NETWORK_TESTS=1`.

The schedule is `.github/workflows/bump-factory-pin.yml`: Mondays 07:00 UTC and
on demand, a read-only resolve job followed by a one-line pull request against
`testing` opened with `peter-evans/create-pull-request` -- the same mechanism
the ISO documentation PR already uses, under the same never-merge rule. Both
jobs check out `testing` once and run the script from that checkout, so a
`workflow_dispatch` fired before `scripts/bump-factory-pin.py` has reached
`testing` fails on the missing file: wait for the sync, or dispatch from a ref
that already carries the script.

That pull request arrives with no checks on it. `create-pull-request` authors
it as `github-actions[bot]` using the default `GITHUB_TOKEN`, and GitHub does
not fire `on: pull_request` workflows for that token; this repository holds no
App or PAT credential to author it with instead. The build matrix is therefore
a manual step -- push an empty commit to `automation/factory-pin`, or close and
reopen the pull request, and `build.yml` runs. An empty check list on one of
these is not a passing build.

Renovate is not the mechanism here because the pin is an `ARG` indirection, not
a `FROM image@sha256:` -- the built-in dockerfile manager cannot see it, and
the org custom manager only covers `image-versions.yml`. A future
`image-versions.yml` in this repository would make that a duplicate; do not
add one without retiring this workflow.

## Verification

```bash
just check
~/.local/bin/pre-commit run actionlint --all-files
```
