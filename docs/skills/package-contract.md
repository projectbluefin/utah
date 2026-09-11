---
name: package-contract
version: "1.1"
last_updated: "2026-09-11"
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
  Bluefin parity contract: machine-readable effective payload bluefin.toml,
  utah.toml overlay, [unavailable] rules, repository policy. Use when adding,
  removing, or debugging packages or parity/check-repos failures.
metadata:
  type: policy
---

# Package Contract

Utah keeps Bluefin's user-facing package contract on a Hummingbird base. Two
manifests under `packages/` define what the image installs; this skill is the
policy for changing them.

## The two manifests

- **`packages/bluefin.toml`** — the parity contract. It is a machine-readable
  contract derived from the package transactions that Bluefin actually executes:
  base Fedora packages, version-specific additions, external/COPR transactions
  (such as `tailscale` and `uupd`), and multimedia transactions (including
  overrides and codecs). **Never hand-edit it without validation.**
  `just check-parity` runs `scripts/check-parity.py` on every CI run, deriving
  the effective payload from upstream Bluefin's `build_files/packages/base.toml`
  and `build_files/base/03-packages.sh` to detect additions and removals.
- **`packages/utah.toml`** — Utah's overlay. Everything Utah needs *in
  addition to* or *instead of* the contract lives here. The full rules are in
  the header comment of that file (cite it; do not move or copy it):

  - `[gnome]` — GNOME 51 desktop contract Hummingbird does not ship.
  - `[build]` — toolchain needed to build the pinned GNOME extensions
    (`scripts/build-gnome-extensions.sh`).
  - `[services]` — desktop services Hummingbird's server base does not install
    (`fwupd`, `systemd-resolved`).
  - `[unavailable]` — Bluefin contract packages none of Utah's repositories
    provide.

## [unavailable] rules

`[unavailable]` means "no source provides this name at all". Each entry
**MUST carry a tracking issue**: the list is the documented parity debt, not
a dumping ground for packages that are merely inconvenient (header comment,
`packages/utah.toml`).

## Multimedia transactions and overrides

Bluefin executes an extensive multimedia transaction combining negativo17's
`fedora-multimedia` repository overrides (`mesa`, `libva`, etc.) and codecs
(`ffmpeg`, `gstreamer1-plugins-*`, `lame`, `libfdk-aac`, `libjxl`). Utah does
not enable negativo17 or Fedora runtime repositories.

Where Hummingbird or `utah-packages` already provides the required build
(`intel-gmmlib`, `intel-mediasdk`, `intel-vpl-gpu-rt`, `libheif`, `libva`),
Utah installs it directly as part of the contract. Packages not yet available
in Utah's enabled repositories are recorded in `[unavailable]` with tracking
issue #12, until the complete multimedia closure from `utah-packages#24` is
published.

## Repository policy

Runtime repositories are the pinned `utah-packages` repository (listed first)
plus Hummingbird's own repository only. **Fedora repositories are never
enabled at runtime** — they are bootstrap material for the package factory's
buildroot, not a source of installed packages (Containerfile package-RUN
comment, `Containerfile` ~L94; repo files copied at `Containerfile` L40).
`Containerfile.kernel`'s builder stage may use the pinned Fedora 44 repository
(`packages/fedora-44.repo`) strictly as a builder-only toolchain.

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

- A contract package missing from every repository the image enables is a
  **build failure**: the dnf transaction in `install-packages.py` exits
  non-zero and the Containerfile RUN step fails. `just check-repos` exists to
  fail fast on that case — names only, no versions — instead of discovering
  it twenty minutes into a build (recipe comment, `Justfile`, `check-repos`;
  needs network access).
- `[unavailable]` entries still present in the install set are a validation
  error (`install-packages.py --check`).
- Drift in `packages/bluefin.toml` from upstream is a CI failure
  (`just check-parity`).

Current counts: 64 Bluefin contract packages installed (58 fedora base +
1 fedora_v44 + 5 multimedia overrides), 12 Utah additions (10 GNOME 51,
2 desktop services), 28 documented as unavailable.

## Verification

```bash
just check-parity
just check-repos
python3 scripts/install-packages.py --check packages/bluefin.toml
python3 scripts/verify-rpm-contract.py --check packages/bluefin.toml
```
