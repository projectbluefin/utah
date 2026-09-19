---
name: package-contract
version: "1.0"
last_updated: "2026-09-18"
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
  Bluefin parity contract: verbatim bluefin.toml, utah.toml overlay,
  [unavailable] rules, repository policy. Use when adding, removing, or
  debugging packages or parity/check-repos failures.
metadata:
  type: policy
---

# Package Contract

Utah keeps Bluefin's user-facing package contract on a Hummingbird base. Two
manifests under `packages/` define what the image installs; this skill is the
policy for changing them.

## The two manifests

- **`packages/bluefin.toml`** — the parity contract. It is a byte-for-byte
  copy of projectbluefin/bluefin's `build_files/packages/base.toml` and must
  stay that way. **Never hand-edit it.** Sync it verbatim from upstream; any
  drift is a parity bug. `just check-parity` diffs it against upstream on
  every CI run so drift fails the build rather than accumulating quietly
  (recipe comment: `Justfile`, `check-parity`).
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
  - `[services]` — desktop services Bluefin adds on top of the server base.
  - `[unavailable]` — Bluefin contract packages none of Utah's repositories
    provide, plus any `[multimedia_overrides]` name the factory has not yet
    published (see below).

## [unavailable] rules

`[unavailable]` means "no source provides this name at all". Each entry
**MUST carry a tracking issue**: the list is the documented parity debt, not
a dumping ground for packages that are merely inconvenient (header comment,
`packages/utah.toml`).

## multimedia_overrides are consumed from the factory

Bluefin's `[multimedia_overrides]` (twelve names: mesa-libGL,
mesa-vulkan-drivers, libva, intel-mediasdk, libheif and friends) are **not**
extra packages. They are the same names Fedora already ships, which Bluefin
*replaces* with negativo17 builds by enabling `fedora-multimedia`. Utah does
not enable that repository, so it takes the same names from the utah-packages
factory instead.

`scripts/install-packages.py` adds `[multimedia_overrides]` to the install
transaction and versionlocks every override it installs, so the factory
builds are pinned and cannot be silently swapped for Fedora's or negativo17's.
`scripts/verify-multimedia.py` then asserts each installed override carries the
factory (Hummingbird `hum`) release rather than a substitute build, and probes
the VA-API codec path. Source identity is read from the release tag, not a
pinned NEVRa, because exact versions move with the factory on every rebuild
(verify-multimedia.py:~19).

The factory has published five of the twelve for Hummingbird
(intel-gmmlib, intel-mediasdk, intel-vpl-gpu-rt, libheif and libva). The
remaining seven -- the whole Mesa family plus libva-intel-media-driver -- are
not yet published, so none of Utah's repositories provide them and they are
listed under `[unavailable]` (tracked by utah-packages#24). A name is not
silently skipped: it is documented, and it returns to the asserted contract
the moment the factory publishes it. When the factory overlay is complete
these move from `[unavailable]` into the version assertion, so the contract
asserts factory versions for every override, not just the five.

`scripts/verify-multimedia.py --check packages/bluefin.toml packages/utah.toml`
validates the manifest off-image (non-empty overrides, no duplicates).

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
- Drift in `packages/bluefin.toml` from upstream is a CI failure
  (`just check-parity`).

Current counts, per `install-packages.py --check`: 78 Bluefin parity packages
resolved (58 from `[fedora]`, 5 of Bluefin's 12 `[multimedia_overrides]` from
the factory, plus GNOME 51 and desktop services), 16 documented as unavailable
(parity debt, each with a tracking issue).

## Verification

```bash
just check-parity
just check-repos
python3 scripts/install-packages.py --check packages/bluefin.toml
python3 scripts/verify-rpm-contract.py --check packages/bluefin.toml
python3 scripts/verify-multimedia.py --check packages/bluefin.toml packages/utah.toml
```
