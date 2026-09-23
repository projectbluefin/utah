---
name: package-contract
version: "1.1"
last_updated: "2026-09-20"
id: package-contract
one_line_purpose: Maintain Bluefin package parity, supply-chain attestation, and repository policy.
entry_point: docs/skills/package-contract.md
category: contracts
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [packages, parity, bluefin, contracts, supply-chain, provenance]
description: >-
  Bluefin parity contract: verbatim bluefin.toml, utah.toml overlay,
  supply-chain NEVRA attestation, repository allowlist, and build provenance.
  Use when modifying packages, repository policy, or resolving parity failures.
metadata:
  type: policy
---

# Package Contract

Utah keeps Bluefin's user-facing package contract on a Hummingbird base. Two
manifests under `packages/` define what the image installs; this skill is the
policy for changing them and asserting supply-chain integrity.

## The two manifests

- **`packages/bluefin.toml`** — the parity contract. It is a byte-for-byte
  copy of projectbluefin/bluefin's `build_files/packages/base.toml` pinned to
  the revision in `packages/.bluefin-parity-ref` and must stay that way.
  **Never hand-edit it.** Sync it verbatim from upstream; any drift is a parity
  bug. `just check-parity` diffs it against upstream on every CI run so drift
  fails the build rather than accumulating quietly (recipe comment: `Justfile`,
  `check-parity`).
- **`packages/utah.toml`** — Utah's overlay. Everything Utah needs *in
  addition to* or *instead of* the contract lives here. The full rules are in
  the header comment of that file (cite it; do not move or copy it):

  - `[gnome]` — GNOME 51 desktop contract Hummingbird does not ship.
  - `[gnome.versions]` — required major versions for GNOME contract packages.
  - `[build]` — toolchain needed to build the pinned GNOME extensions
    (`scripts/build-gnome-extensions.sh`).
  - `[parity]` — what Bluefin inherits from Fedora's base image and Hummingbird
    has in its repository but not in its bootable base; CI's package
    availability step resolves the real transaction and is the gate on every
    name there.
  - `[services]` — desktop services Bluefin adds on top of the server base.
  - `[factory]` — packages expected from the factory rebuild with `.bfin` release identity.
  - `[repositories]` — explicitly allowed runtime RPM repositories.
  - `[unavailable]` — Bluefin contract packages none of Utah's repositories
    provide.

## [unavailable] rules

`[unavailable]` means "no source provides this name at all". Each entry
**MUST carry a tracking issue**: the list is the documented parity debt, not
a dumping ground for packages that are merely inconvenient (header comment,
`packages/utah.toml`).

## multimedia_overrides are not missing packages

Bluefin's `[multimedia_overrides]` (twelve names: mesa-libGL,
mesa-vulkan-drivers, libva, intel-mediasdk, libheif and friends) are **not**
extra packages. They are the same names Fedora already ships, which Bluefin
*replaces* with negativo17 builds by enabling `fedora-multimedia`. Utah does
not enable that repository, so it installs Fedora's builds instead. Nothing
is absent from the image; what differs is which build it carries, and the
practical consequence is hardware-accelerated codec support.

That is why they are absent from the contract rather than listed under
`[unavailable]`: recording them as missing would be wrong (a source does
provide the name), and recording them as satisfied would hide a real
functional difference. The factory already builds several of them in
projectbluefin/hummingbird-github; when that overlay is published and enabled
here, these can move into the contract as a version assertion rather than a
name one (header comment, `packages/utah.toml`).

## Repository policy and allowlist

Runtime repositories are the pinned `utah-packages` repository (listed first)
plus Hummingbird's own repository only, along with `nvidia-container-toolkit`
for container GPU acceleration. **Fedora repositories are never enabled at
runtime** — they are bootstrap material for the package factory's buildroot,
not a source of installed packages (Containerfile package-RUN comment,
`Containerfile` ~L94; repo files copied at `Containerfile` L40).
`Containerfile.kernel`'s builder stage may use the pinned Fedora 44 repository
(`packages/fedora-44.repo`) strictly as a builder-only toolchain.

The install-source identity is single-sourced in `packages/*.repo`. Each repository
participating in the package install transaction carries a `# utah-install: true`
annotation (either directly preceding or within the `[section]` header in
`packages/utah-packages.repo` and `packages/hummingbird.repo`).
`scripts/install-packages.py` derives the `--enablerepo` set from these annotations
ordered by priority (ascending), so rebuilds in `utah-packages` (`priority=1`)
precede base Hummingbird packages (`priority=10`). Repositories without this marker
(such as `nvidia-container-toolkit` or builder-only `fedora-44`) are excluded from
the desktop package transaction.

`scripts/verify-rpm-contract.py` enforces a final repository allowlist over the
whole runtime DNF configuration: every `*.repo` file in each directory DNF
actually reads, plus any repository section declared directly in
`/etc/dnf/dnf.conf` or `/etc/dnf/libdnf5.conf`. With no explicit `reposdir` that
means DNF's own default list — `/etc/yum.repos.d`, `/etc/yum/repos.d` and
`/etc/distro.repos.d` — not `/etc/yum.repos.d` alone, so an enabled repository
file cannot be hidden from the attestation by placing it in one of the other
directories DNF reads. An explicit `reposdir=` in either configuration file
replaces that default list, matching DNF's semantics, and every directory it
names is scanned instead. Any enabled Fedora repository (`fedora`,
`fedora-updates`, etc.) or unapproved third-party repository causes the contract
verification to fail immediately. `UTAH_POLICY_ROOT` re-roots the scan, which is
how the unit tests attest a known filesystem rather than the DNF configuration
of whatever machine runs them.

The pinned package image is an RPM repository, not a runtime dependency: its
contents are copied into the image so the package transaction is reproducible
and does not depend on a mutable mirror (`Containerfile` L41-44).

## Supply-chain attestation and build provenance

Beyond package-name presence, `scripts/verify-rpm-contract.py` validates NEVRA
attributes and source provenance for every contract package:

1. **GNOME major version attestation**: Desktop packages (`gnome-shell`,
   `mutter`, `gnome-control-center`, `gnome-session`, `gnome-settings-daemon`,
   `gsettings-desktop-schemas`, `xdg-desktop-portal-gnome`) must declare major
   version `51`, matching the GNOME 51 contract.
2. **Factory release identity**: Packages expected from the package factory
   rebuild must carry the factory release identity (`.bfin`, e.g. `.hum1.bfin`).
   Bluefin parity packages expected from the factory cannot silently resolve
   from Hummingbird or Fedora repositories. Which packages those are is stated
   once, in `[factory]` in `packages/utah.toml`, and the verifier derives the
   GNOME split from it rather than naming exceptions in code. Because `.bfin` is
   applied per build job in `projectbluefin/utah-packages`, it marks what the
   factory built: a name belongs in `[factory]` only if the factory keeps a
   recipe for it. The sources it declares Hummingbird-owned (`bootc`, `dracut`,
   `firewalld`, `gcc`, `libxcrypt`, `make`, `openssh`, `rust-bootupd`) have no
   recipe and are pruned from its published repository, so they are excluded
   from `[factory]` and asserted absent by the tests.
3. **Hummingbird release identity**: Packages provided by Hummingbird must carry
   `.hum` release identity and cannot resolve from raw Fedora packages (`.fc`).
4. **Build provenance retention**: The resolved package-origin and NEVRA report
   is written to `/usr/share/utah/package-origins.json` and
   `/usr/share/utah/package-origins.txt`, recording the exact NEVRA, epoch,
   architecture, and repository origin with build metadata in the final image.
   Retention is enforced: if the report cannot be written the verifier fails,
   because a build that kept no report has proven nothing about its origins.
   `UTAH_REPORT_DIR` redirects the report elsewhere, which is how the unit
   tests exercise the writer without touching the host.

## Supply-chain download verification

Every executable release asset fetched during image or ISO composition is
version-pinned and verified against a committed digest or published checksum
before extraction or execution. `scripts/check-download-integrity.py` runs
in `just check` and pre-commit to statically enforce that no build recipe
resolves a mutable latest release or downloads unverified executables.

## Install and verify cannot disagree

`scripts/install-packages.py` records exactly what its run resolved to
`/usr/share/utah/contract.txt`, and `scripts/verify-rpm-contract.py` asserts
*that file* in an image build rather than recomputing the set — the two
drifted once, so a contract package was installed and never verified
(install-packages.py:~125, verify-rpm-contract.py:~60). The manifest path in
the verifier is only the off-image `--check` fallback and asserts nothing
about installation.

On NVIDIA flavors (`IMAGE_FLAVOR=nvidia` or `nvidia-gaming`),
`scripts/verify-rpm-contract.py` also asserts that the kernel module
(`extra/nvidia/nvidia.ko`) is present for every bootable kernel in the image and
that userspace tools (`nvidia-smi`, `nvidia-driver-version`) exist. Determining
the base kernel release cannot rely solely on `rpm -q kernel`, because `kernel`
is a metapackage that may not be installed on a minimal bootc base, and rpm queries
may return nothing or unhelpful text such as `package kernel is not installed`. If
no release resolves from rpm (empty or whitespace output), or if the resolved
release does not correspond to a directory under `/usr/lib/modules/<release>`, the
verifier falls back to the module trees present on disk under `/usr/lib/modules/`
(excluding the OGC gaming release for the base check). Furthermore, the verifier
guards against empty release strings, refusing to construct module paths from empty
releases or emit missing-module errors with empty kernel names.

## Failure semantics

- A contract package or dependency missing from the installation transaction
  is a **build failure**. `just check-repos` reads the base and package-image
  digests from `Containerfile`, copies the same repository configuration, and
  runs `install-packages.py --resolve` inside that base. This includes the
  release-specific Bluefin section, GNOME, services, and extension build tools.
  It needs Podman and network access. A name lookup on GitHub Pages is not
  evidence that the pinned OCI repository is complete or ABI-compatible.
  The factory's leading metadata layer is checked against the pinned manifest
  and blob hashes, then mounted for resolution without downloading RPMs. The
  factory must copy the same repodata into the leading layer and payload layer.
  DNF's `--assumeno` may return 1 for a valid declined transaction; the checker
  requires a transaction summary and rejects dependency and repository errors.
- A package failing the required GNOME major version (e.g. not GNOME 51) or
  carrying an unapproved release identity is a **build failure**.
- A factory package resolving from Hummingbird or Fedora without `.bfin` is a
  **build failure**.
- An unapproved or Fedora repository enabled at runtime is a **build failure**.
- `[unavailable]` entries still present in the install set are a validation
  error (`install-packages.py --check`).
- Drift in `packages/bluefin.toml` from upstream at `packages/.bluefin-parity-ref`
  is a CI failure (`just check-parity`).

## Pinned upstream parity reference

`packages/.bluefin-parity-ref` holds the 40-character commit SHA that
`packages/bluefin.toml` is synchronized with. The reference exists so Utah's
parity gate tests against a known revision rather than moving with Bluefin's
default branch, preventing unrelated upstream changes from breaking Utah's CI.
Update it whenever synchronizing `packages/bluefin.toml` with upstream.

Current counts, per the README "Package parity" section: 58 Bluefin contract
packages installed, 44 Utah additions (GNOME 51, base-image parity, desktop
services), 9 genuinely unavailable. `scripts/check-doc-counts.py` (part of
`just check`) recomputes these from the manifests and fails if either
document drifts from `site/data/packages.json`.

## Verification

```bash
just check-parity
just check-repos
python3 scripts/install-packages.py --check packages/bluefin.toml
python3 scripts/verify-rpm-contract.py --check packages/bluefin.toml
python3 scripts/check-doc-counts.py
```
