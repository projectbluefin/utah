---
name: flavors
version: "1.0"
last_updated: "2026-09-18"
id: flavors
one_line_purpose: Add, remove, or retire an image flavor safely.
entry_point: docs/skills/flavors.md
category: image-build
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [flavors, matrix, workflows]
description: >-
  config/flavors.json is the single source for build, promote, and release
  matrices. Use when changing the flavor set or debugging a matrix that
  names an image nothing builds.
on_device: docs/agent/skills/flavors.md
metadata:
  type: procedure
---

# Flavors

Utah builds four `x86_64` flavors -- `main`, `nvidia`, `gaming`,
`nvidia-gaming` -- matching Bluefin's, on a single `testing` stream.
`config/flavors.json` decides which images exist, and everything derives from
it: the build matrix, the promote matrix, the release matrix, and whether the
kernel cache image is built at all.

```bash
python3 scripts/flavors.py list          # ["main", "nvidia", "gaming", "nvidia-gaming"]
python3 scripts/flavors.py image nvidia  # utah-nvidia
python3 scripts/flavors.py needs-kernel  # true
```

## Retire a flavor through the `retired` map

A flavor is switched off by removing it from `flavors` and recording why
under `retired`, which keeps the reason beside the list rather than in a
commit message. Nothing else has to change: the scripts, the Containerfile
stages, and the cache image all stay put either way. `retired` is not
commentary -- it is what a flavor moves to when it is switched off
(docstring, `scripts/flavors.py`).

## No workflow or Justfile literals

`just check` fails if any workflow or Justfile recipe names `utah-nvidia` or
`utah-gaming` directly (`Justfile`, `check` recipe; the guards are
`! grep -rnE 'utah-(nvidia|gaming)' .github/workflows/` and
`! grep -nE '(utah|\{\{ image \}\})-(nvidia|gaming)' Justfile`). No workflow
or recipe may carry its own copy of the flavor list or image names -- that drift
is what `config/flavors.json` and `scripts/flavors.py` exist to stop. Recipes
that need an image name ask for it at the source -- `build-ghcr`, `gen-sbom`,
`secureboot`, and `scripts/check-reproducible-build.sh` each run
`python3 scripts/flavors.py image <flavor>` -- so
build, promote, and release always agree on published image names. `just
image_name` is the same query wrapped for callers outside the Justfile; no
recipe goes through it.

## The matrix is split in two

The build workflow resolves the set in its "Resolve the image flavors" step
and submits it as two matrices, `build_main` and `build_kernel` (comment,
`.github/workflows/build.yml`). Every flavor builds on the kernel cache image:
`main` and `nvidia` for its patched Fedora kernel, the rest for the OGC kernel
too ([kernel-cache.md](kernel-cache.md#the-patched-fedora-kernel)). So both
matrices wait for the cache job. They stay separate because the reusable
workflow keys its cancel-in-progress concurrency group on `brand_name`, so the
two calls carry different names, and the required check is named after
`build_main`. Utah's Justfile ignores `brand_name` when naming images.

## flavors.py renders the set in the shape each consumer needs

`scripts/flavors.py` is the only reader of `config/flavors.json`:

- `list` -- every active flavor, for the build matrix.
- `list-main` -- `main`, submitted as its own matrix (`build_main`).
- `list-kernel` -- the OGC and NVIDIA flavors (`build_kernel`).
- `suites` -- each flavor's image with its configured testsuite suites,
  read from the `suites` map in `config/flavors.json` (the `testsuite` matrix
  in `post-testing-e2e.yml`). A flavor with no `suites` entry is a hard error,
  so a new flavor needs its suites declared alongside it.
- `needs-kernel` -- `true` whenever any flavor is active, since every flavor
  builds on the kernel cache.
- `images` / `releases` -- the same set shaped for the promote and release
  matrices (docstring, `scripts/flavors.py`).
- `image FLAVOR` -- the published image name for a flavor (e.g. `utah` or
  `utah-nvidia`), called directly by the recipes that build, tag, or publish
  images, and exposed to outside callers as `just image_name`.

Unknown names in `flavors` are a hard error at read time, so a typo in the
config fails before any matrix is built from it.

## Verification

```bash
python3 scripts/flavors.py list
just check
```
