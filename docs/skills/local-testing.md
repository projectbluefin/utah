---
name: local-testing
version: "1.0"
last_updated: "2026-09-05"
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
  live ISO. Use when validating changes end-to-end or debugging boot, GDM, or
  live-session failures.
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
integration is the next ISO milestone. `just boot-iso` boots it with
QEMU-for-Docker and exposes the noVNC console at the printed URL (comment
above `boot-iso` in `Justfile`), with TPM, UEFI, and `-snapshot` so nothing
persists.

## Verification

### Encrypted install and screenshot harness

`just luks-test` runs `iso/scripts/luks-e2e.sh` against a debug live ISO
(`just iso testing 1`). It checks the live GNOME session, installs to a
disposable LUKS2 disk from the embedded payload, boots without the ISO,
unlocks the disk, and checks graphical login and extension states.
Read the recipe and script prerequisites before running it: it creates test
accounts and requires local QEMU/KVM access, not a production installation.

Passing runs refresh `docs/verification/README.md`, its screenshots, and the
delimited verification block in the root README. These are historical local
test records, not proof that the current commit passed CI. In particular,
local fastfetch capture waits after terminal autostart by default. CI sets
`UTAH_E2E_REQUIRE_FASTFETCH=1` to require OCR of its completion marker and
kernel output, and `UTAH_E2E_REQUIRE_SCREENSHOTS=1` to reject missing PNGs.
CI retains the tested commit/image digest and proposes evidence updates in a
documentation PR only after all flavors pass. See [ci-workflows.md](ci-workflows.md).

The harness blocks outbound guest networking while retaining loopback-only
SSH forwards, so installation cannot silently fall back to an online pull.
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
