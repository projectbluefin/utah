---
name: local-testing
version: "1.0"
last_updated: "2026-11-26"
id: local-testing
one_line_purpose: Build, install, and boot Utah locally in a VM or with the gnome-setup install medium.
entry_point: docs/skills/local-testing.md
category: testing
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [qemu, bootc, installer, vm, testing]
description: >-
  Local validation loop: build-ghcr, bootc install to-disk, QEMU/noVNC boot,
  the gnome-setup install medium, and Secure Boot strategy. Use when validating
  changes end-to-end or debugging boot, GDM, or live-session failures.
metadata:
  type: runbook
---

# Local testing

The local loop builds the image, installs it to a disposable disk, and boots
that disk in a VM with a graphical console in the browser. The gnome-setup
install medium covers the install path. Everything runs from the Justfile; the recipe
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

## Secure Boot strategy

Every Utah image and the install medium currently need Secure Boot **off**. The
owner decided on 2026-11-20 that Utah gets signed through Hummingbird and
Fedora upstream, never with a Utah key or MOK enrollment the way Dakota does.
The per-component inventory, the evidence and the upstream route for each
piece are in
[kernel-cache/references/secure-boot.md](kernel-cache/references/secure-boot.md).
In short: Fedora shim (Microsoft UEFI CA) loads Fedora GRUB (Fedora CA), which
is valid, then refuses the kernel, because every flavor boots Utah's rebuilt
Fedora kernel (`*.utah1`) or the unsigned OGC kernel; and the out-of-tree
modules (NVIDIA open, v4l2loopback) are unsigned, so lockdown refuses them.

## Install medium (gnome-setup)

Utah installs with GNOME STF's gnome-setup driving systemd-sysinstall (owner
decision, #1569): a raw bootc disk image that boots live with `utah.live` and
installs by block copy into LUKS2. It is the install Post-Testing E2E gates
`:testing` on, for every flavor. The fisherman live ISO (`iso/live/`,
`luks-e2e.sh`, `lifecycle-e2e.sh`) and the tacklebox ISO were removed with
it.

```bash
sudo just build-ghcr utah testing main   # the image, in root's storage
sudo just medium testing                 # -> output/medium.raw
```

Boot `output/medium.raw` read-only next to a blank target disk of at least 48
GB, or write it to a USB stick. Build, VM steps, the CI harness
(`iso/raw/raw_e2e.py`: live boot, GUI install, TPM2 and recovery-key unlock,
gnome-initial-setup, `bootc switch` and `rollback`), its installed-system
checks (`iso/scripts/e2e-services.sh` and the others it runs in the guest:
[installed-system checks](local-testing/references/image-installer.md#installed-system-checks-ported-from-the-fisherman-harness))
and gotchas: [image-installer](local-testing/references/image-installer.md).
A candidate in CI: dispatch `build.yml` with `contract_only` and `review_vm`
([review VM](local-testing/references/iso-review.md)).
