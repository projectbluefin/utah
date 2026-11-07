---
name: kernel-cache
version: "1.0"
last_updated: "2026-11-07"
id: kernel-cache
one_line_purpose: Understand and rebuild the kernel cache image (patched Fedora kernel, OGC kernel, NVIDIA module).
entry_point: docs/skills/kernel-cache.md
category: image-build
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [kernel, nvidia, ogc, cache, btrfs, fsverity]
description: >-
  The kernel cache image, its content-hash tag, and unpack-or-compile
  behavior. Use when touching install-fedora-kernel.sh, kernel-patches,
  install-ogc-kernel.sh, install-nvidia.sh, Containerfile.kernel, or
  debugging flavored builds.
metadata:
  type: reference
---

# Kernel Cache

Every flavor builds on the kernel cache image. It carries three things no
repository ships for this base:

- **The distribution kernel, patched.** Hummingbird takes its kernel from
  Fedora 44 (`fedora-44-updates`), and every kernel from 7.2 on -- 7.3.x and
  7.4-rc1 included -- reports intact btrfs fs-verity data as corrupt. See
  [The patched Fedora kernel](#the-patched-fedora-kernel). `main` needs this as
  much as the rest.
- **The OGC kernel** for `gaming` and `nvidia-gaming`.
- **NVIDIA's open module** for `nvidia` and `nvidia-gaming`.

That is about an hour for the Fedora kernel rebuild, half an hour for the OGC
kernel and several minutes for the module, and none of it depends on what the
image build does -- only on the pinned base image, the patches in
`packages/kernel-patches/` and the three install scripts.

So it is paid once, in a separate image built from `Containerfile.kernel` and
tagged with a hash of exactly those inputs:

```bash
just kernel-cache-tag     # the hash
just kernel-cache-ref     # ghcr.io/<owner>/utah-kernel-cache:<hash>
just build-kernel-cache
```

CI reuses a published cache only when its digest is signed by this repository's
`build.yml` (GitHub OIDC issuer; `refs/heads/testing` and same-repo PR merge
refs only).
Misses and unsigned or wrongly signed hits are built, pushed, signed and
verified. The consumer resolves the content tag once, requires cosign, verifies
that immutable digest with the same identity regexp, and passes only those
verified bytes as `BASE_IMAGE`. A missing verifier is an error, not a skip.
A fork pull request cannot publish the cache, so no flavor is built for it.

## The patched Fedora kernel

`btrfs_verify_folio()` asks fs-verity to verify the whole folio when a read
completes only part of it. Since 7.2 btrfs uses large data folios by default,
so the partial read is common: verification hashes blocks that have not
arrived, the kernel logs `fs-verity (dm-0, inode N): FILE CORRUPTED!` with
`real_hash` equal to the hash of a zero block, and the read is retried --
usually successfully, sometimes as `EIO` in userspace. A composefs root is
nothing but fs-verity objects, so a composefs install on btrfs (`bootc install
--composefs-backend`, or bootc-migrate's OSTree to composefs conversion) reads
its own system files through the bug. The upstream fix is linux-next
`dcf8811f63af` ("btrfs: fix the fsverity callback where unexpected range is
verified"); no released kernel carries it yet (bootc-dev/bootc#2471).

`scripts/install-fedora-kernel.sh build` rebuilds Fedora's own source RPM for
the base image's kernel (Koji's copy signed with the Fedora 44 key, also
checked against a recorded SHA-256) with `packages/kernel-patches/*.patch`
concatenated into the spec's `linux-kernel-test.patch` hook. Base variant only:
no debug kernels, debuginfo, UKI, tools or headers. The release gains
`.1.utah1` (`7.2.15-200.1.utah1.fc44`), which sorts above Fedora's build and
below any later one.

The builder installs the result, with its `kernel-devel`, before the NVIDIA
step, so the module is built for the kernel the images boot. The final stage
installs it into the base image itself (`install`, which also removes Fedora's
tree), so everything built `FROM` the cache -- the Containerfile's
v4l2loopback stage included -- sees it as the base kernel. `kernel-devel`
stays in `/utah-cache/fedora-kernel/`, where `install-v4l2loopback.sh` and
`install-nvidia.sh` take it before trying Koji. The Containerfile runs
`utah-install-fedora-kernel check` first, so a build on the pristine
Hummingbird base fails instead of shipping the bug.

A/B evidence for the patch (2026-11-07, VM, 6700 fs-verity files on btrfs, 10
cold-read passes each): Fedora `7.2.15-200.fc44` logged 18 `FILE CORRUPTED`
reports, at least one on every pass; the rebuild logged none.

Remove it when the fix is in the base image's kernel: delete the patch (and the
script, its `COPY`s, the `check` in the Containerfile and the
`/utah-cache/fedora-kernel` lookups), and `main` can return to the pristine
base. Moving `BASE_IMAGE` to a new kernel before then means moving
`FEDORA_KERNEL_VERSION`, `FEDORA_KERNEL_RELEASE` and
`FEDORA_KERNEL_SRPM_SHA256` with it; the `check` fails the build until they
agree. Secure Boot: the rebuild is not signed by Fedora's key, so with Secure
Boot enforcing, shim refuses it just as it refuses the OGC kernel.

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
  when its tag is not already published, every flavor would silently take an
  image built from the previous recipe.
- `scripts/install-fedora-kernel.sh`, `scripts/install-ogc-kernel.sh` and
  `scripts/install-nvidia.sh`, whole files, comments included -- the scripts
  that do the building. The Fedora kernel's version, release and source RPM
  digest are pinned in the first.
- `packages/kernel-patches/*.patch` -- what the Fedora kernel is rebuilt with.
- `packages/hummingbird.repo` and `packages/fedora-44.repo` -- the repositories
  the toolchain comes from (Fedora 44 is builder-only); a different compiler
  produces a different kernel.
- `packages/RPM-GPG-KEY-redhat-release-2` and
  `packages/RPM-GPG-KEY-fedora-44-primary` -- the keys the builder verifies
  Hummingbird's RPMs and the Fedora kernel source RPM with.

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

The gaming desktop also depends on two OGC config options that the x86_64
defconfig omits: Flatpak's Bubblewrap sandbox needs unprivileged user
namespaces (`CONFIG_USER_NS`), and Tailscale needs the TUN device
(`CONFIG_TUN`). A gaming VM boot exposed both gaps in `7.1.8-ogc1`: user
namespace creation returned `EINVAL`, and Tailscale could not load `tun`.
`install-ogc-kernel.sh` now checks both in `required_config`, so a cached or
fresh OGC kernel fails during image build instead of losing these desktop
features at runtime.

The testsuite drives key combos through `/dev/uinput`, which the defconfig
also omits: the guest has no uinput device even after `modprobe uinput`, so
every uinput step errors (smoke-b "Settings closes cleanly via Ctrl+Q" on the
gaming flavors, #741). The script builds it as a module, as Fedora ships it,
and gates on it like the options above.

zram is missing too, but nothing errors outright: zram-generator asks for
`/dev/zram0`, systemd waits 50s for `dev-zram0.device`, and the late boot
pushes `geoclue.service` past its start timeout ("No failed systemd units at
boot" on the gaming flavors, #741). A serial log with `Timed out waiting for
device dev-zram0.device` means this. Built as a module with `ZSMALLOC`, gated.

The contract also covers the display. `x86_64_defconfig` enables DRM and the
Intel i915 driver and nothing else, so on any other GPU, QEMU's included, the
OGC kernel registered no DRM device: GDM started, mutter had no KMS device to
open, and the gaming flavors failed the ISO test on a black screen with
`no gnome-shell running for liveuser` while `main`, on the distribution
kernel, came up through `simpledrm`. The script enables `SYSFB_SIMPLEFB` and
`DRM_SIMPLEDRM` (the firmware framebuffer as a KMS device on any hardware),
plus `DRM_VIRTIO_GPU` and `DRM_BOCHS` for VMs, and gates on `DRM_SIMPLEDRM`
like the filesystem options.

Native GPU drivers for real gaming hardware are built as modules: `DRM_AMDGPU`
(with Display Core, `DRM_AMD_DC`) for AMD GPUs and `DRM_XE` for Intel Arc and
Battlemage, both gated in `required_config`. `DRM_NOUVEAU` is omitted because
the NVIDIA flavors use the proprietary open module. `DRM_CLIENT_SELECTION` and
`DRM_FBDEV_EMULATION` are enabled so `drm_fb_helper` exports
`drm_fb_helper_set_suspend_unlocked`, maintaining build compatibility with the
out-of-tree NVIDIA driver module when `DRM_TTM_HELPER` is selected by amdgpu/xe.
At runtime, these drivers require their corresponding firmware (`amd-gpu-firmware`,
`intel-gpu-firmware`), which the image carries via `linux-firmware` in `packages/utah.toml`.
When diagnosing display issues on real hardware, verify that the installed gaming
flavor initializes its native driver (`[drm] Initialized amdgpu` in the journal)
rather than falling back to `simpledrm`. When diagnosing a black live session,
compare `live-serial.log` for `[drm] Initialized` lines between the `main` and `gaming`
diagnostics artifacts before reading GDM logs.

```bash
just kernel-cache-tag
just check
```
