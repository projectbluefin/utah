---
name: package-contract
version: "1.0"
last_updated: "2026-09-05"
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

The pinned package image is an RPM repository, not a runtime dependency: its
contents are copied into the image so the package transaction is reproducible
and does not depend on a mutable mirror (`Containerfile` L41-44).

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

Current counts, per the README "Package parity" section: 61 Bluefin contract
packages installed, 12 Utah additions (GNOME 51, desktop services), 4
genuinely unavailable.

## Verification

```bash
just check-parity
just check-repos
python3 scripts/install-packages.py --check packages/bluefin.toml
python3 scripts/verify-rpm-contract.py --check packages/bluefin.toml
```
