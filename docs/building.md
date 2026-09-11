# Building Utah

Contributor quickstart: the local build loop, testing procedures, Dakota-style
quality gates, and artifact verification. The deep documentation lives in the
skills linked at the bottom; the README is the user-facing page.

## Build locally

```bash
just check
just build-ghcr utah testing main
just generate-bootable-image testing
just boot-vm

# Optional local diagnostics over SSH (never use for a published image):
ENABLE_SSHD=1 just build-ghcr utah testing main

# Compose against a local package repository image:
just build-local testing localhost/utah-packages:local-merged

# Build the live ISO with offline installer payload:
just iso testing
just boot-iso
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

## Dakota-style quality gates

Utah adopts Dakota's staged validation topology to ensure reliability and
parity before artifacts are published or promoted:

### 1. Preflight contract gate
Static checks run before expensive builds:
- `just check` — validates manifests, workflow step mappings, download integrity,
  extension metadata, desktop contract syntax, and skill catalog freshness.
- `just check-parity` — asserts byte-for-byte identity between `packages/bluefin.toml`
  and upstream Bluefin `base.toml`.
- `just check-repos` — verifies every contract package is present in enabled
  runtime repositories (Hummingbird base and `utah-packages` factory).

### 2. Image and kernel build gate
- Split matrix: `build_main` builds directly on the clean Hummingbird base;
  flavored builds (`build_kernel`) build on the content-hashed kernel cache image
  (`scripts/kernel-cache-tag.sh`), building on demand on cache miss.
- Layer discipline and caching: OCI layer caches are read-only on PRs and
  updated only on pushes to `testing`.

### 3. Artifact verification
- **RPM contract**: `scripts/verify-rpm-contract.py` verifies the exact package
  list recorded at install time in `/usr/share/utah/contract.txt`.
- **Desktop contract**: `scripts/verify-desktop-contract.py` validates identity
  (`NAME=Utah`, `ID=utah`, `VERSION_CODENAME=Utahraptor`), branding assets,
  dconf profiles, and service presets (`gdm.service`, `systemd-resolved`, etc.).
- **GNOME extensions**: `scripts/verify-gnome-extensions.py` ensures all 9 bundled
  extensions declare GNOME 51 support in `metadata.json`.
- **Download integrity**: `scripts/check-download-integrity.py` ensures all
  executable downloads (uupd, NVIDIA drivers, Flatpaks) are pinned by checksum.
- **Supply chain**: CI generates cosign keyless signatures, SBOMs, and provenance
  attestations via `projectbluefin/actions@v1`.

### 4. End-to-end VM and installer testing
- **Disk VM validation**: `just generate-bootable-image testing && just boot-vm`
  performs a full `bootc install to-disk` to a sparse disk image and boots QEMU
  to verify graphical target and GDM.
- **Live ISO & offline installer validation**: `just iso testing && just boot-iso`
  assembles a UEFI live ISO with systemd-boot, embedding the `bootc-installer`
  Flatpak bundle (`org.bootcinstaller.Installer`) and the target image in a VFS
  `containers-storage` graphroot for offline installation. Booting validates the
  `UTAH_LIVE_READY` serial marker and offline installer execution.
- **CI E2E gate**: `.github/workflows/post-testing-e2e.yml` verifies built
  digest artifacts before advancing the `:testing` tag.

### 5. Promotion and rollback lifecycle
- **Stream promotion**: Images land first on `:testing`. After passing post-testing
  E2E validation, `.github/workflows/promote-testing-to-main.yml` promotes testing
  to `main`, updating `:stable` tags.
- **Branch synchronization**: `.github/workflows/sync-main-to-testing.yml` syncs
  protected `main` back into `testing` on every merge.
- **Host rollbacks**: Deployed client systems roll back atomically via native
  `bootc rollback`.

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
