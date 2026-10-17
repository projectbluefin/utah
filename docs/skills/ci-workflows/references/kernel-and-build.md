# Kernel cache and build matrix

The kernel cache job runs only when `needs_kernel` is `true` -- while the
matrix is main-only, building it is 45 minutes spent on an image nothing
consumes (comment, `.github/workflows/build.yml`). When it does run, it
frees runner disk, logs in to GHCR with `GITHUB_TOKEN`, and probes the
content-hash tag with `podman pull`. A published tag is a cache hit only if
its digest verifies; unsigned or wrongly signed hits are rebuilt from the
checkout, pushed, signed and verified just like misses. What the tag hashes
and why lives in [kernel-cache.md](../../kernel-cache.md).

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

`cosign verify` pins the issuer and identity to this workflow and accepts
only `refs/heads/testing` and `refs/pull/N/merge`. Manual dispatches may
publish only from `testing`; branch previews must set `contract_only` and
therefore skip the kernel cache and reusable image publishers. A same-repo
pull request may still publish a kernel-cache image under its `pull/N/merge`
identity (#316).

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
`reusable-build.yml@ae7d740d261d56354aeaf0a72af6f87e442e0472 # v1`, and
`just check` asserts that pin with
`grep -qE 'reusable-build\.yml@(v1|[0-9a-f]{40} # v1)$' .github/workflows/build.yml`
(recipe, `Justfile`, `check`). Both pass `publish_stream_tag: "false"` --
testing is advanced only after post-testing-e2e validates the build
(comment, `.github/workflows/build.yml`). Both set `rechunk: "true"` to
opt the testing stream into rechunking and build SBOMs, which
reusable-build skips by default. That opt-in requires the reusable
workflow's `rechunk` input added in projectbluefin/actions#557 (which
ae7d740d261d56354aeaf0a72af6f87e442e0472 includes); `workflow_call` validates
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
cache narrative lives in [containerfile.md](../../containerfile.md).

## promote-testing-to-main

A `variants` job reads the same `scripts/flavors.py images` list the build
matrix does -- promoting an image the build no longer produces fails late
and confusingly, so this reads the same list (comment,
`.github/workflows/promote-testing-to-main.yml`). The `promote` job calls
`reusable-promote-squash.yml` at the caller's pinned commit against
`ghcr.io/<owner>`, with a cosign identity regexp matching the pinned
`projectbluefin/actions/.github/workflows/reusable-build.yml` path at an
immutable 40-hex commit SHA and `run_e2e: true`, with
`e2e_suites: smoke,common,nvidia`. Push-to-`testing` and scheduled runs remain
enabled; a manually dispatched run may promote only from the trusted `main`
ref. A source commit is eligible for branch promotion only after
`post-testing-e2e.yml` publishes trusted `e2e/post-testing` status on that
exact SHA; the status follows the LUKS, production-ISO, full flavor testsuite,
and testing-tag promotion gates.

## sync-main-to-testing

Source pushes to `main` and the nightly schedule call
`reusable-sync-branches.yml@v1`, then explicitly dispatch `build.yml` on
`testing` with `actions: write`. README/verification-only pushes are excluded
to avoid evidence-update build loops. Nightly runs still sync those changes.

The sync workflow passes the current full `testing` SHA. Before checkout, the
contract job rejects any non-empty `sha` input that differs from the run's
immutable `github.sha`, and rejects publisher dispatches from refs other than
`testing`. Therefore the `inputs.sha` preflight/cache tree and the reusable
build's `github.sha` tree cannot diverge; if the branch moves during dispatch,
the contract fails before either image publisher can run.
