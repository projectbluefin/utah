---
name: kernel-cache
version: "1.0"
last_updated: "2026-09-05"
id: kernel-cache
one_line_purpose: Understand and rebuild the OGC kernel and NVIDIA module cache image.
entry_point: docs/skills/kernel-cache.md
category: image-build
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [kernel, nvidia, ogc, cache]
description: >-
  The kernel cache image, its content-hash tag, and unpack-or-compile
  behavior. Use when touching install-ogc-kernel.sh, install-nvidia.sh,
  Containerfile.kernel, or debugging flavored builds.
metadata:
  type: reference
---

# Kernel Cache

`gaming`, `nvidia` and `nvidia-gaming` need an OGC kernel and an NVIDIA
kernel module that no repository ships for this base, so Utah compiles them.
That is about half an hour for the kernel and several minutes for the module,
and it depends on nothing the image build does -- only on the pinned base
image and on `scripts/install-ogc-kernel.sh` and `scripts/install-nvidia.sh`.

So it is paid once, in a separate image built from `Containerfile.kernel` and
tagged with a hash of exactly those inputs:

```bash
just kernel-cache-tag     # the hash
just kernel-cache-ref     # ghcr.io/<owner>/utah-kernel-cache:<hash>
just build-kernel-cache
```

CI builds and pushes the cache image only when that tag is not already
published, and the three flavors that need it use it as their base image;
`main` uses the pristine Hummingbird base and pulls none of it (step "Build
the kernel cache image if it is not published yet", `.github/workflows/build.yml`;
recipe comment, `Justfile`, `build-ghcr`).

## What the tag hashes

The tag must change whenever anything the cache image contains would change,
and not otherwise, so it is a hash of exactly those inputs (header comment,
`scripts/kernel-cache-tag.sh`):

- The `ARG BASE_IMAGE=` line of `Containerfile.kernel` -- the base image the
  cache is built from.
- `scripts/install-ogc-kernel.sh` and `scripts/install-nvidia.sh`, whole
  files, comments included -- the two scripts that do the building.
- `packages/hummingbird.repo` -- the repositories the toolchain comes from;
  a different compiler produces a different kernel.

Editing either script changes the hash and forces a rebuild, comment-only
edits included. That is deliberate: the key can only ever rebuild something
that did not need rebuilding, never reuse something stale. A cheaper key
that hashed just the version pins would miss a change to how the kernel is
configured or how the module is linked (header comment,
`scripts/kernel-cache-tag.sh`).

The `BASE_IMAGE` in `Containerfile.kernel` must match the one in
`Containerfile`, or the prebuilt NVIDIA module would be linked against a
kernel the image never boots. Two literals, one invariant, so `just check`
asserts it with a diff of both `ARG BASE_IMAGE=` lines rather than trusting
it (recipe comment, `Justfile`, `check`).

## Unpack or compile

The cache image is the same base image plus a `/utah-cache` directory of
archives (comment, `Containerfile.kernel`). The install scripts find the
archives under `/utah-cache` and unpack them. With no cache present -- a
local `just build-ghcr`, or the cache image's own build -- the same scripts
compile from source, so there is no second implementation to drift.

`nvidia-gaming` is the superset: it produces a module for the base kernel
and one for the OGC kernel, so a single cache image serves all three flavors
(comment, `Containerfile.kernel`).

## Verification

```bash
just kernel-cache-tag
just check
```
