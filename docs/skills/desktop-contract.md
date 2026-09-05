---
name: desktop-contract
version: "1.0"
last_updated: "2026-09-05"
id: desktop-contract
one_line_purpose: Maintain Utah identity, Bluefin desktop defaults, and first-boot Flatpak policy.
entry_point: docs/skills/desktop-contract.md
category: contracts
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [desktop, branding, gnome, flatpak]
description: >-
  The runtime desktop contract in contracts/bluefin-desktop.toml and its
  in-image verifiers. Use when changing branding, os-release, service
  presets, GNOME extensions, or first-boot Flatpak behavior.
metadata:
  type: policy
---

# Desktop Contract

`contracts/bluefin-desktop.toml` is the runtime contract for Utah's
Bluefin-derived desktop experience on top of Hummingbird. It is intentionally
separate from `packages/bluefin.toml`: package parity proves RPMs; this file
proves Utah identity, Bluefin desktop defaults, and first-boot Flatpak policy
(header comment, `contracts/bluefin-desktop.toml`). The contract is data; two
verifiers enforce it — `scripts/verify-desktop-contract.py` for the TOML
itself and `scripts/verify-gnome-extensions.py` for the bundled extensions.

## What the contract asserts

The TOML's sections are the contract's table of contents:

- **`[branding]`** — files that must exist (Bluefin logos, backgrounds, the
  `zz0-bluefin-modifications` gschema override, fastfetch and Bazaar count
  files) plus the os-release identity. The identity fields are exact values:
  `NAME=Utah`, `ID=utah`, `ID_LIKE=fedora`, `VERSION_CODENAME=Utahraptor`,
  `DEFAULT_HOSTNAME=utah`, `IMAGE_ID=utah`, and the projectbluefin.io URLs.
  `[branding.os_release_patterns]` shapes the fields the build generates:
  `PRETTY_NAME` is `Utah (Version: ...)`, `VERSION` carries `(Hummingbird)`,
  `VARIANT_ID` starts with `utah`. The verifier reads `/usr/lib/os-release`.
- **`[branding.image_info]`** — `/usr/share/ublue-os/image-info.json` must
  name image `utah`, vendor `projectbluefin`, base `hummingbird`, with the
  flavor pattern `(main|nvidia|gaming|nvidia-gaming)` and the matching
  `ostree-image-signed` ref pattern.
- **`[configuration]`** — the dconf distro databases and locks under
  `/etc/dconf/db/distro.d/` must exist, and `file_contains` pins their
  content: the gschema override references Bazaar and the Bluefin background
  path, the custom command menu points at `docs.projectbluefin.io`, the
  keybindings set `xdg-terminal-exec`.
- **`[flatpak]`** — first-boot policy: the Flathub remote
  (`https://dl.flathub.org/repo/`), the Bazaar preinstall, the
  `99-flatpaks.sh` privileged-setup hook, and the system-flatpaks Brewfile
  whose app list the contract enumerates.
- **`[services]`** — systemd units the preset must enable: `gdm.service`,
  `ublue-system-setup.service`, `flatpak-preinstall.service`,
  `flatpak-nuke-fedora.service`, `brew-setup.service`, `dconf-update.service`,
  `bootc-unified-storage.service`, `uupd.timer`.

## GNOME extensions are pinned submodules

Bluefin's GNOME extension submodules are retained with their normal build
step. `.gitmodules` pins nine of them by URL and branch under
`system_files/shared/usr/share/gnome-shell/extensions/` — appindicator,
bazaar-integration, blur-my-shell, caffeine, custom-command-list,
dash-to-dock, gradia-integration, gsconnect, and search-light.

`scripts/verify-gnome-extensions.py` asserts every one declares GNOME 51 in
its `metadata.json`. It runs in two modes from the same script:

- **Source mode** — `--source` checks the submodule checkouts in the working
  tree; this is what `just check` runs.
- **Installed mode** — the default checks `/usr/share/gnome-shell/extensions`
  under an image root; this is what runs in the Containerfile as
  `/usr/local/libexec/utah-verify-gnome-extensions`, right after
  `utah-build-gnome-extensions`.

## Services and login defaults

Hummingbird defaults to a server preset and disables unlisted services, so
the desktop policy is applied explicitly. `scripts/configure-services.sh`
mirrors bluefin-lts's `40-services.sh`: it applies the desktop presets,
enables GDM, firmware updates, Tailscale, uupd, user setup and resolved,
configures authselect, and removes the extension build toolchain before
cleanup (Containerfile RUN comment and `docs/building.md` design bullets).

## The verifiers run twice

The same verifier runs in the Containerfile and on demand, so a local image
or a CI artifact can be checked after the fact (recipe comment, `Justfile`,
`check-desktop-contract`):

- **In the image build** — the desktop RUN step ends with
  `utah-verify-desktop-contract /usr/share/utah/bluefin-desktop.toml`, after
  branding and services are configured; a contract failure fails the build.
  The extension verifier runs earlier in the same step.
- **On demand** — `just check-desktop-contract <ref>` (default
  `localhost/utah:testing`) podman-runs both verifiers inside an
  already-composed image: the desktop verifier and the contract are
  bind-mounted from the working tree, the extension verifier runs from the
  image's own `/usr/local/libexec`.
- **Off-image** — `verify-desktop-contract.py --check` validates the contract
  TOML itself in source-only CI and is part of `just check`; it asserts
  nothing about any image.

## Verification

```bash
python3 scripts/verify-desktop-contract.py --check contracts/bluefin-desktop.toml
python3 scripts/verify-gnome-extensions.py --source
just check-desktop-contract localhost/utah:testing  # requires a locally built image
```
