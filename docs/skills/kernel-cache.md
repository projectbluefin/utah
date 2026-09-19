---
name: kernel-cache
version: "1.0"
last_updated: "2026-09-19"
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

- `Containerfile.kernel`, whole file -- the recipe that builds the cache. Its
  `ARG BASE_IMAGE=` line names the base image, but its body is what decides
  what the image ends up containing: the repo files copied into the builder,
  and the single `RUN` that fills `/cache-out`, and the final stage's
  `COPY --from=builder`. Hashing only the `ARG` line would leave an edit to
  any of that invisible to the key, and since CI builds the cache image only
  when its tag is not already published, the three cached flavors would
  silently take an image built from the previous recipe.
- `scripts/install-ogc-kernel.sh` and `scripts/install-nvidia.sh`, whole
  files, comments included -- the two scripts that do the building.
- `packages/hummingbird.repo` and `packages/fedora-44.repo` -- the repositories
  the toolchain comes from (Fedora 44 is builder-only); a different compiler
  produces a different kernel.

Editing any of them changes the hash and forces a rebuild, comment-only
edits included. That is deliberate: the key can only ever rebuild something
that did not need rebuilding, never reuse something stale. A cheaper key
that hashed just the version pins would miss a change to how the kernel is
configured or how the module is linked (header comment,
`scripts/kernel-cache-tag.sh`).

`tests/test_kernel_cache_tag.py` holds the key to that contract: it asserts
that mutating a non-`ARG` line of `Containerfile.kernel`, or any of the four
other hashed files, moves the tag.

The `BASE_IMAGE` in `Containerfile.kernel` must match the one in
`Containerfile`, or the prebuilt NVIDIA module would be linked against a
kernel the image never boots. Two literals, one invariant, so `just check`
asserts it with a diff of both `ARG BASE_IMAGE=` lines rather than trusting
it (recipe comment, `Justfile`, `check`).

That hashed list used to be maintained by hand against a second list --
what `Containerfile.kernel` actually builds from -- with nothing tying the
two together. A `COPY` added there without a matching line in the hash
script yields a key that does not move when that input does, and because CI
skips the rebuild whenever the tag is already published, the three flavors
that consume the cache keep unpacking a kernel and a module built from the
old input for as long as the tag stays put.

`tests/test_kernel_cache_key.py` closes that: it derives the input set from
`Containerfile.kernel` (the `ARG BASE_IMAGE=` line and every non-`--from`
`COPY` source) rather than restating it, then mutates a copy of the tree and
re-runs `scripts/kernel-cache-tag.sh` to assert each derived input moves the
key, that an unrelated file (`scripts/flavors.py`) does not, and that the key
is deterministic. Add a `COPY` to `Containerfile.kernel` and the suite fails
until `scripts/kernel-cache-tag.sh` hashes it too. It runs in `just test`,
and so in `just check`.

## Unpack or compile

The cache image is the same base image plus a `/utah-cache` directory of
archives (comment, `Containerfile.kernel`). The install scripts find the
archives under `/utah-cache` and unpack them. With no cache present -- a
local `just build-ghcr`, or the cache image's own build -- the same scripts
compile from source, so there is no second implementation to drift.

`nvidia-gaming` is the superset: it produces a module for the base kernel
and one for the OGC kernel, so a single cache image serves all three flavors
(comment, `Containerfile.kernel`).

The vendor installer is verified against `NVIDIA_RUN_SHA256`, a digest
committed in `install-nvidia.sh` beside `KERNEL_DEVEL_SHA256`, on both the
download path and the `/utah-cache` path — the cache image is addressed by an
input-hash tag, not an immutable digest, so a cached installer gets the same
check as a fresh one. Bumping `UTAH_NVIDIA_DRIVER_VERSION` means updating
`NVIDIA_RUN_SHA256` with it, from NVIDIA's published
`NVIDIA-Linux-x86_64-<version>.run.sha256sum` (comment, `install-nvidia.sh`).

## Verification

The OGC kernel must satisfy the live ISO and installed-root contract, not only
gaming features. `x86_64_defconfig` omits OverlayFS and SquashFS; dracut's
`dmsquash-live` depends on `overlayfs`, and Utah's live image uses zstd SquashFS.
The installer also exercises LUKS/device-mapper, while OSTree needs its
filesystem support. `install-ogc-kernel.sh` enables these explicitly and checks
the same `required_config` list after `olddefconfig`, after installation, and
on cache extraction. Never omit a dracut module to work around a missing kernel
feature.

The installer also needs btrfs: the LUKS ISO test formats and mounts the root
on `btrfs`, and the OGC kernel's `x86_64_defconfig` omits `CONFIG_BTRFS_FS`, so
the gaming flavors' live kernel had no btrfs module -- `mkfs.btrfs` succeeded
and the mount then failed with `unknown filesystem type 'btrfs'`, the same
missing `/dev/btrfs-control` as a bare module, which looked like a bad
filesystem at install time. `install-ogc-kernel.sh` enables `BTRFS_FS` as a
module beside SquashFS/EROFS and asserts it in `required_config`, so the
absence fails the build rather than the install. `iso/scripts/luks-e2e.sh`
adds a `modprobe btrfs` pre-check before the installer runs, turning a repeat
into a one-line diagnosis. Changes to this contract invalidate the cache
through the existing script hash and require a real kernel rebuild plus the ISO
boot tests.

The contract also covers the display. `x86_64_defconfig` enables DRM and the
Intel i915 driver and nothing else, so on any other GPU, QEMU's included, the
OGC kernel registered no DRM device: GDM started, mutter had no KMS device to
open, and the gaming flavors failed the ISO test on a black screen with
`no gnome-shell running for liveuser` while `main`, on the distribution
kernel, came up through `simpledrm`. The script enables `SYSFB_SIMPLEFB` and
`DRM_SIMPLEDRM` (the firmware framebuffer as a KMS device on any hardware),
plus `DRM_VIRTIO_GPU` and `DRM_BOCHS` for VMs, and gates on `DRM_SIMPLEDRM`
like the filesystem options. Native drivers for real gaming GPUs (amdgpu, xe)
are not enabled yet: they need linux-firmware in the image, which the install
set does not carry (projectbluefin/utah#97). When diagnosing a black live
session, compare `live-serial.log` for `[drm] Initialized` lines between the
`main` and `gaming` diagnostics artifacts before reading GDM logs.

```bash
just kernel-cache-tag
just check
```
