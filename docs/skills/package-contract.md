---
name: package-contract
version: "1.0"
last_updated: "2026-09-22"
id: package-contract
one_line_purpose: Maintain Bluefin package parity and Utah's overlay manifest.
entry_point: docs/skills/package-contract.md
category: contracts
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [packages, parity, bluefin, contracts]
description: >-
  Bluefin parity contract: verbatim bluefin.toml, utah.toml overlay, device
  firmware, [unavailable] rules, repository policy. Use when adding, removing,
  or debugging packages or parity/check-repos failures.
metadata:
  type: policy
---

# Package Contract

Utah keeps Bluefin's user-facing package contract on a Hummingbird base. Two
manifests under `packages/` define what the image installs; this skill is the
policy for changing them.

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
  - `[build]` — toolchain needed to build the pinned GNOME extensions
    (`scripts/build-gnome-extensions.sh`).
  - `[parity]` — what Bluefin inherits from Fedora's base image and Hummingbird
    has in its repository but not in its bootable base; CI's package
    availability step resolves the real transaction and is the gate on every
    name there.
  - `[hardware]` — device firmware the bootable base leaves out entirely. Its
    own section rather than `[parity]`, because it is parity with nothing:
    Hummingbird's repository has no `linux-firmware` to inherit, the factory
    builds it, and nothing in the image `Requires` it. `linux-firmware` is a
    split package whose thirteen Recommends omit Intel wireless, so the iwlwifi
    and iwlegacy packages are named explicitly — the X230's
    `iwlwifi-6000g2a-6.ucode` ships in `iwlwifi-dvm-firmware` (#97).
  - `[services]` — desktop services Bluefin adds on top of the server base.
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

## Repository policy

Runtime repositories are the pinned `utah-packages` repository (listed first)
plus Hummingbird's own repository only. **Fedora repositories are never
enabled at runtime** — they are bootstrap material for the package factory's
buildroot, not a source of installed packages (Containerfile package-RUN
comment, `Containerfile` ~L94; repo files copied at `Containerfile` L40).
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

The pinned package image is an RPM repository, not a runtime dependency: its
contents are copied into the image so the package transaction is reproducible
and does not depend on a mutable mirror (`Containerfile` L41-44).

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

Current counts, per the README "Package parity" section: 57 Bluefin contract
packages installed, 79 Utah additions (GNOME 51, base-image parity, device
firmware, desktop services), 10 genuinely unavailable. `scripts/check-doc-counts.py` (part of
`just check`) recomputes these from the manifests and fails if either
document drifts from `site/data/packages.json`.

## A section nobody reads installs nothing

`contract()` in `scripts/install-packages.py` composes the install set from an
explicit list of overlay sections. A section added to `packages/utah.toml` but
not named there is read by nobody: the build succeeds, `just check` passes, and
the packages silently never install — the same failure this contract exists to
catch, one level up. Adding a section means four edits, not one: the section
and its header entry in `packages/utah.toml`; `contract()`; both buckets in
`scripts/verify-rpm-contract.py` (the `/usr/share/utah/contract.txt` path and
the off-image fallback) and its printed count; and `GROUPS` in
`scripts/generate-site-data.py`, which is what puts the card on the status
page. `tests/test_package_resolution.py` and `tests/test_verify_rpm_contract.py`
assert a package in every section reaches the install set and is counted under
its own heading, so a section wired into one place and not another fails the
suite rather than shipping quietly.

## Verification

```bash
just check-parity
just check-repos
python3 scripts/install-packages.py --check packages/bluefin.toml
python3 scripts/verify-rpm-contract.py --check packages/bluefin.toml
python3 scripts/check-doc-counts.py
```
