---
name: local-testing
version: "1.0"
last_updated: "2026-09-23"
id: local-testing
one_line_purpose: Build, install, and boot Utah locally in a VM or live ISO.
entry_point: docs/skills/local-testing.md
category: testing
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [qemu, bootc, iso, vm, testing]
description: >-
  Local validation loop: build-ghcr, bootc install to-disk, QEMU/noVNC boot,
  live ISO boot paths, and Secure Boot strategy. Use when validating changes
  end-to-end or debugging boot, GDM, or live-session failures.
metadata:
  type: runbook
---

# Local testing

The local loop builds the image, installs it to a disposable disk, and boots
that disk in a VM with a graphical console in the browser. A live ISO path
covers initial bring-up. Everything runs from the Justfile; the recipe
comments cited below are the canonical detail -- cite them, do not copy them.

## Build and boot the installed disk

The exact command sequence:

```bash
just check
just build-ghcr utah testing main
just generate-bootable-image testing
just boot-vm
```

`just check` is the gate CI runs first, and it needs the GNOME extension
submodules initialized -- run `git submodule update --init --recursive` once,
or the extension contract check fails with missing `metadata.json` errors
(AGENTS.md, build section).

`just build-ghcr utah testing main` tags the image `localhost/utah:testing`.
`just generate-bootable-image testing` then uses `bootc install to-disk` to
create `output/bootable.raw`; this follows Bluefin's bootc-to-disk path rather
than trying to boot an OCI layer directly (comment above
`generate-bootable-image` in `Justfile`). What the recipe does around the
install, per its comments:

- bootc needs the host root namespace, and local image builds normally use
  rootless Podman, so the recipe transfers the image into rootful storage
  once before running the disk install.
- The disk is a 30G sparse file, installed with `--filesystem btrfs --wipe
  --generic-image`.
- When the developer's `~/.ssh/id_ed25519.pub` exists, it is injected as a
  root key for headless boot diagnostics. It does not enable password login
  and is only present in the disposable locally generated disk, never in the
  OCI image.
- bootupd writes the vendor EFI entry but a fresh QEMU VM has no NVRAM entry,
  so the recipe installs the standard removable-media fallback
  (`EFI/BOOT/BOOTX64.EFI` plus Fedora's `grubx64.efi` beside it) so QEMU
  firmware can find the image without importing host firmware variables.

`just boot-vm` runs that disk with `ghcr.io/qemus/qemu` and serves the
graphical console at the printed URL (comment above `boot-vm` in `Justfile`).
The disk is mounted at `/boot.img` and `-snapshot` keeps the test disposable.
The recipe prints the noVNC port (8006, auto-incremented if busy) and an SSH
port (2222, likewise). Success is: GDM appears and the GNOME Shell desktop
renders in the noVNC web console.

Override `BASE_DIR`, `VM_RAM`, or `VM_CPUS` when needed -- they are Justfile
variables read from the environment (defaults `output`, `8192`, `4`).

## Upgrade and rollback lifecycle

Utah manages immutable image updates through `uupd.timer` rather than legacy
`rpm-ostree` or background `bootc-fetch-apply-updates.timer`. When testing
lifecycle upgrades or switches (such as switching from Bluefin via `bootc switch`),
`bootc-fetch-apply-updates.timer` must remain masked: an active fetch-apply timer
stages candidate images automatically in the background outside uupd policy,
which can inadvertently undo a user rollback (`bootc rollback`) upon subsequent
reboot (links #17, #101).

## Composing with local packages

When iterating on package builds locally before publication, use:

```bash
just build-local testing localhost/utah-packages:local-merged
```

This runs the production `Containerfile` against a package repository image
already present in local containers-storage via `PACKAGE_IMAGE_REF`.

## Local-only SSH diagnostics

Published images keep SSH disabled. Set `ENABLE_SSHD=1` only for a local
diagnostic build; this follows TunaOS's debug-image convention and makes the
disposable root key used by `boot-vm` useful:

```bash
ENABLE_SSHD=1 just build-ghcr utah testing main
```

Never use it for a published image. With such a build booted, `boot-vm`'s
printed `ssh -p <port> root@127.0.0.1` line works; without it, only the web
console is available.

## The `utah.local` karg

The local OCI ref is not available from the guest's localhost registry, so
`generate-bootable-image` marks a `localhost/*` disk with `--karg=utah.local`
(comment in `generate-bootable-image`, `Justfile`): the published-image-only
unified-storage service is skipped instead of retrying its registry repull
forever. Published images omit that argument and keep the service enabled.

## Live ISO (initial bring-up)

The first ISO slice reuses Utah's own kernel, dracut-live, and GNOME image.
`just iso` builds a single-architecture UEFI live ISO; this first slice proves
the Utah live boot path, and installer payload integration is intentionally
the next ISO milestone (comment above `iso` in `Justfile`):

```bash
just iso testing
just boot-iso        # QEMU/noVNC live-session validation
just iso testing 1   # optional live-session SSH diagnostics (debug=1)
```

The result is `output/utah-live.iso`, assembled with systemd-boot, a
`UTAH_LIVE` dmsquash-live root, and a serial `UTAH_LIVE_READY` marker. It is
intended to prove live desktop boot first; bootc-installer/offline payload
integration is the next ISO milestone. `luks-e2e.sh`'s live-boot gate asserts
the `UTAH_LIVE_READY` marker on the serial console in addition to
`graphical.target`, because the marker is written only by the
`utah-live-ready.service` oneshot that runs after
`display-manager.service` has started. `just boot-iso` boots it with
QEMU-for-Docker and exposes the noVNC console at the printed URL (comment
above `boot-iso` in `Justfile`), with TPM, UEFI, and `-snapshot` so nothing
persists.

### Supported and unsupported boot paths

- **UEFI x86_64 (Supported)**: The live ISO is built strictly for UEFI boot
  using `systemd-boot` located at `EFI/BOOT/BOOTX64.EFI` on the FAT32 ESP.
  Dracut locates the live squashfs root via `root=live:LABEL=UTAH_LIVE`.
  This is the only supported and tested live boot path.
- **Legacy BIOS/CSM (Unsupported)**: Legacy MBR boot is unsupported. The ISO
  omits MBR boot sectors and isolinux binaries.
- **File-backed / Ventoy ISO loopback (Unsupported)**: Tools such as Ventoy
  or GRUB loopback rely on initramfs hooks to mount the ISO container file from
  a host filesystem before pivoting to the live root. Utah does not implement
  custom dracut loopback handlers or a `rd.utah.isofile` locator; `loopback.cfg`
  is deliberately omitted from the ISO to avoid advertising a non-functional
  boot path. Media must be written directly to physical drives (e.g. via
  Fedora Media Writer, `dd`, or balenaEtcher).

### Kernel arguments and SELinux enforcement

Production live boot entries configure:
`root=live:LABEL=UTAH_LIVE rd.live.image rd.live.overlay.overlayfs=1 enforcing=0 console=ttyS0,115200n8`

- Every kernel argument corresponds to an implemented and tested dracut boot
  path (`dmsquash-live`, `overlayfs`, and serial console logging).
- **SELinux policy (Documented Exception)**: The live session runs with
  SELinux in **Permissive** mode (`enforcing=0`) as an approved documented
  exception (Issue #22). The squashfs live rootfs is assembled rootless inside
  `podman unshare`, where `security.selinux` extended attributes cannot be
  written without root privilege, leaving rootfs files unlabeled. Booting an
  unlabeled live root in Enforcing mode causes denials in systemd and GDM that
  hang the live session. Permissive mode remains in place for live media until
  xattr-preserving squashfs build tooling lands. Installed target systems boot
  in **Enforcing** mode.

### Secure Boot strategy

- **Live ISO bootloader**: The live image installs `systemd-boot-unsigned`.
  On hardware with Microsoft UEFI Secure Boot enabled, firmware will reject the
  unsigned EFI loader unless Secure Boot is temporarily disabled in UEFI setup.
  Production releases will incorporate Fedora's signed shim (`shimx64.efi`) and
  a signed bootloader binary.
- **Custom flavor kernels (`gaming`, `nvidia-gaming`)**: The OGC gaming kernel
  (`linux-ogc`) is compiled from source and unsigned. Secure Boot systems
  require either disabling Secure Boot or manually enrolling a Machine Owner Key
  (MOK) into UEFI NVRAM using `mokutil` (planned tooling; no automated helper
  currently exists in-tree).
- **Custom flavor modules (`nvidia`, `nvidia-gaming`)**: Out-of-tree NVIDIA
  kernel modules compiled against the base or OGC kernel run under kernel
  lockdown when Secure Boot is active. Unsigned modules fail to load; signing
  modules with an enrolled MOK key (e.g. via the kernel's `sign-file` utility)
  is planned for future release pipelines, but currently module signing is not
  implemented in-tree and Secure Boot must remain disabled.

`iso/live/src/install-flatpaks.sh` pins the bootc-installer Flatpak bundle to
a specific `tuna-os/bootc-installer` release rather than resolving
`/releases/latest/download/` the way dakota-iso does: the bundle installs
system-wide with `--no-gpg-verify`, so the version pin is the whole trust
story, and a fixed tag keeps ISO composition reproducible. Its releases are
tagged by build date + commit sha (e.g. `v2026.09.19-cee9ba29`), not semver,
so there is no tag-name continuity to lean on when bumping it.
`UTAH_INSTALLER_VERSION` and `UTAH_INSTALLER_SHA256` must move together —
take the digest from that release's `org.bootcinstaller.Installer.flatpak`
asset (`digest` field of `gh api repos/tuna-os/bootc-installer/releases/tags/<tag>`,
or download and `sha256sum` it) rather than guessing or reusing an old value.

## Tacklebox ISOs (unpublished variants)

`just iso-tacklebox` builds a live ISO via
[tacklebox](https://github.com/tuna-os/tacklebox) (systemd-boot + tbox-live,
no anaconda) for any `config/flavors.json` flavor -- including variants this
project publishes no ISO for. Two stages: `iso/live/Containerfile.tacklebox`
pre-bakes the live Flatpaks rootless (flatpak's bwrap sandbox needs the user
namespace that tacklebox's rootful customize containers lack), then tacklebox
assembles the ISO with `configure-live.sh` as the `live_customize` script and
the published image ref embedded as an offline payload, so the live desktop
matches the standard ISO:

```bash
just iso-tacklebox main                              # from localhost/utah:testing
just iso-tacklebox main testing ghcr testing-20260922-256d837
```

The script (`iso/scripts/build-iso-tacklebox.sh`) resolves flavored image
names through `scripts/flavors.py image` -- no literals, enforced by
`just check` -- requires root (loop devices, mkfs), and writes
`output/utah-<flavor>-tacklebox.iso`. Tacklebox itself comes from
`ghcr.io/tuna-os/tacklebox:latest` unless `TACKLEBOX_FROM_SOURCE=1`; a host
binary wins when present (`TACKLEBOX_BIN`, or `tacklebox` on `PATH`), which
matters on hosts where nested podman breaks container DNS (observed: the
customize container's resolver unreachable from inside the tacklebox
container while identical host-level runs resolve fine).

Two hard differences from `just iso`:

- **Secure Boot: unsupported.** Tacklebox emits an unsigned systemd-boot
  chain. These ISOs boot with Secure Boot disabled only.
- **Kargs are `enforcing=0 console=ttyS0,115200n8`**, same rationale as the
  standard live ISO (unlabeled squashfs root, serial E2E).

The same images build in the browser at
[iso.tunaos.org](https://iso.tunaos.org) via `?image=` presets -- no Utah-side
registration exists because none is needed: `projectbluefin` is on the relay
org allowlist (`ORGS` in `worker/cors-shim.js`, tuna-os/iso-builder), and any
bootable container (kernel under `/usr/lib/modules`, systemd) is accepted.
Shareable form: `https://iso.tunaos.org/?image=projectbluefin/<name>:<tag>`
using a dated published tag (`testing-*`), since Utah publishes no floating
`testing` tag. Verify a preset by confirming the relay mints a pull token for
the scope (`/token?scope=repository:projectbluefin/<name>:pull`).

## Verification

### Encrypted install and screenshot harness

`just luks-test` runs `iso/scripts/luks-e2e.sh` against a debug live ISO
(`just iso testing 1`). It checks the live GNOME session, installs to a
disposable LUKS2 disk from the embedded payload, boots without the ISO,
unlocks the disk, confirms `bootc status` reports the offline embedded
payload (not a network pull) on a guest with no route out, and checks
graphical login and extension states. A trailing check, after login,
confirms every default Flatpak in the Brewfile contract is also present
offline -- deferred past login because it deploys asynchronously on first
boot. Both gates have escape hatches for unblocking a promotion when the
gate itself, rather than the image, is at fault: `UTAH_E2E_FLATPAKS` sets the
expected Flatpak set directly (empty skips the check), and
`UTAH_E2E_PAYLOAD_CHECK=""` skips the booted-image assertion. The booted-image
read falls back to `sudo` when `bootc status --json` returns nothing to the
unprivileged test user.
Read the recipe and script prerequisites before running it: it creates test
accounts and requires local QEMU/KVM access, not a production installation.

Passing runs refresh `docs/verification/README.md`, its screenshots, and the
delimited verification block in the root README. These are historical local
test records, not proof that the current commit passed CI. The harness gates
the terminal autostart release via a trigger file so `installed-desktop.png`
captures a clean desktop state before `installed-fastfetch.png` captures the
terminal overlay, preventing duplicate verification evidence (#240). CI sets
`UTAH_E2E_REQUIRE_FASTFETCH=1` to require OCR of its completion marker and
kernel output, and `UTAH_E2E_REQUIRE_SCREENSHOTS=1` to reject missing PNGs.
CI retains the tested commit/image digest and proposes evidence updates in a
documentation PR only after all flavors pass. See [ci-workflows.md](ci-workflows.md).

Ghostty is the only terminal the harness's fastfetch capture can show, and
this VM never has a GPU (plain stdvga, no `/dev/dri`), so its terminal
autostart entry (`iso/scripts/luks-e2e.sh`) launches it with
`LIBGL_ALWAYS_SOFTWARE=1 MESA_LOADER_DRIVER_OVERRIDE=llvmpipe`. A
`flatpak override --env=GDK_DISABLE=...` looks like the obvious fix but does
not work: Ghostty computes `GDK_DISABLE` from a hardcoded struct and
`setenv(3)`s it with `overwrite=1` right before `gtk_init`, clobbering any
inherited value, and an upstream build on 2026-09-20 dropped `gles-api` from
that struct and broke every flavor's harness run this way (#183). The two
Mesa variables above are never among the ones Ghostty itself sets, so they
are the ones that actually reach the process.

The harness blocks outbound guest networking while retaining loopback-only
SSH forwards, so installation cannot silently fall back to an online pull.
For digest-pinned builds, the builder exports the original registry blobs
through Skopeo's `dir` transport with `--preserve-digests` on both copies.
Do not export from containers-storage or substitute an OCI archive: the
former exports uncompressed layers, while the latter can convert manifest
formats. Either changes the digest and breaks the embedded `image@sha256:...`
reference. Local tag-based builds may export their local containers-storage.
A digest-preserving copy failure must fail ISO composition, not fall back to
a mutable tag.
The live assembler selects a release from `/usr/lib/modules` and uses its
matching initramfs. Kernel-core provides that release's `vmlinuz` in the module
directory, while the OGC installer writes `/boot/vmlinuz-<release>`.
`iso/scripts/live-kernel.py` supports both paths and resolves symlinks inside
the mounted image, never against the host root. Do not use `/boot/vmlinuz` or
another release as a fallback: that can silently pair mismatched boot files.
It enables sshd through a boot argument on the disposable installed disk,
never by rebuilding or changing the published image. `UTAH_E2E_RAM` and
`UTAH_E2E_CPUS` control VM resources (defaults 8192 MiB and four CPUs).

When integrating this harness with newer image-build fixes, retain the
currently verified package-image digest and available-package contract.
The older ISO branch's package pin and exclusions must not replace them.

```bash
just check
```

Then the manual runbook, when the change touches the boot path:

```bash
just build-ghcr utah testing main
just generate-bootable-image testing
just boot-vm     # success: GDM appears and GNOME Shell renders in noVNC
just iso testing
just boot-iso    # success: live session renders; serial shows UTAH_LIVE_READY
```
