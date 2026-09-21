# Building Utah

Contributor quickstart: the local build loop. The deep documentation lives in
the skills linked at the bottom; the README is the user-facing page.

## Build locally

```bash
just check
just build-ghcr utah testing main
just generate-bootable-image testing
just boot-vm

# Optional local diagnostics over SSH (never use for a published image):
ENABLE_SSHD=1 just build-ghcr utah testing main
```

The image is tagged `localhost/utah:testing`. `generate-bootable-image` uses
`bootc install to-disk` to create `output/bootable.raw`; `boot-vm` runs that disk
with `ghcr.io/qemus/qemu` and serves the graphical console at the printed URL.
Confirm that GDM starts and the GNOME Shell desktop renders in the web console.
Override `BASE_DIR`, `VM_RAM`, or `VM_CPUS` when needed. `boot-vm` prints the
noVNC and SSH ports; SSH is available only when the image was built with
`ENABLE_SSHD=1`. The generated local disk carries `utah.local`, which skips
unified-storage's registry repull; published images omit that argument and keep
the service enabled.

## Validate the release-readiness gates

Once `boot-vm` confirms the desktop boots, the rest of the adopted gates run
locally too:

```bash
just iso testing 1              # build a debug live ISO (needed for luks-test)
just boot-iso                   # boot the live session and confirm the installer is there
just luks-test                  # install to an encrypted disk over SSH, boot it, log in
just try-installed              # re-boot the disk luks-test installed, with a browser console
just check-desktop-contract     # branding, services, and Flatpak policy against a built image
just check-repos                # every contract package resolves in the enabled repositories
```

`just luks-test` drives `iso/scripts/luks-e2e.sh`: it installs from the ISO's
embedded container store with no network, unlocks the LUKS2 volume at the
Plymouth prompt, confirms the disk boots on its own, and overwrites
[`docs/verification/README.md`](verification/README.md) and its screenshots
with the fresh passing record — that file documents exactly what a run
proved and when. See
[`skills/local-testing.md`](skills/local-testing.md) for the encrypted and
plain offline install paths and the `bootc rollback` / `uupd` update policy.

## Deep documentation

| Topic | Skill |
| --- | --- |
| Image flavors and the build/promote/release matrices | [`skills/flavors.md`](skills/flavors.md) |
| The kernel cache image | [`skills/kernel-cache.md`](skills/kernel-cache.md) |
| Layer discipline and where the build time goes | [`skills/containerfile.md`](skills/containerfile.md) |
| Local VM and live ISO detail | [`skills/local-testing.md`](skills/local-testing.md) |
| CI workflows | [`skills/ci-workflows.md`](skills/ci-workflows.md) |
| Package contract and the design bullets | [`skills/package-contract.md`](skills/package-contract.md) |
| Desktop contract: branding, services, Flatpak policy | [`skills/desktop-contract.md`](skills/desktop-contract.md) |
