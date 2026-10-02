---
name: setup-hooks
version: "1.0"
last_updated: "2026-09-30"
id: setup-hooks
one_line_purpose: Change a first-boot setup hook without breaking the once-only contract.
entry_point: docs/skills/setup-hooks.md
category: meta
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [hooks, first-boot, version-script, libsetup, bootupd]
description: >-
  First-boot setup hooks stamp completion through libsetup.sh. Use when editing
  a hook, migrating one off the legacy `version-script` gate, or debugging a
  hook that never runs or runs every boot.
metadata:
  type: procedure
---

# Setup hooks

Hooks live in two directories:

- `system_files/shared/usr/share/ublue-os/user-setup.hooks.d/` — runs per user.
- `system_files/shared/usr/share/ublue-os/privileged-setup.hooks.d/` — runs via
  pkexec as root, when a privileged action is needed.

Every hook sources `/usr/lib/ublue/setup-services/libsetup.sh` (shipped by
`projectbluefin/common` through the pinned `COMMON_IMAGE_SHA`).

## The once-only contract

A hook must stamp completion **after** its body succeeds, never before:

```bash
version-script-check <name> <scope> 1 || exit 0   # read-only: have we done this?

set -xeuo pipefail
# body
version-script-commit <name> <scope> 1            # record success
```

`projectbluefin/common#1196` split the old `version-script` helper in two. The
legacy helper recorded the version *before* the body ran, so a hook that failed
on first boot — a transient missing binary, an unreadable DMI node — was
skipped forever after. The new pair leaves the gate read-only and lets the hook
record success itself, so a failure retries on the next boot.

Two consequences to keep in mind when writing the body:

1. **Use `set -e`.** Without it the hook runs on past a failed body and commits
   a completion that never happened.
2. **Split skip paths by whether the condition can change.** A deliberate skip
   (wrong vendor, karg already applied) commits before `exit 0` so the hook
   stops re-running every boot. A transient skip (DMI unreadable, dependency
   missing) exits *without* committing so it retries.

## Compat shim

The pinned common image has no `version-script-check`/`version-script-commit`
until projectbluefin/common#1196 lands. Each hook therefore defines a shim so it
works in either merge order:

```bash
if ! declare -F version-script-check >/dev/null; then
    version-script-check() { version-script "$@"; }
    version-script-commit() { :; }
fi
```

As of this writing `20-home-labels.sh`, `05-bootupctl-adopt.sh` and
`user-setup.hooks.d/30-ghostty.sh` carry the shim. The other Utah hooks
(`10-tailscale.sh`, `11-framework-ucsi-workaround.sh`, `99-flatpaks.sh`,
`user-setup.hooks.d/20-framework.sh`) still call the legacy `version-script`
helper, pending #259.

`30-ghostty.sh` is the case that shows why the legacy gate is not good enough
for a body that moves files: it migrates the user's only real Ghostty config
out of the flatpak's per-app directory, so a body that aborts part-way must get
another attempt rather than burn the stamp on a half-done migration.

## First-boot hook inventory

Every hook in `privileged-setup.hooks.d/` runs in lexicographic order from
`ublue-privileged-setup`. New hooks pick a number lower than 10 so they run
before any of the existing defaults; 05 is the lowest prefix currently in
use.

| Hook | Purpose |
|------|---------|
| `05-bootupctl-adopt.sh` | Run `bootupctl adopt-and-update` on first boot so a switched-into-Utah system sees its on-disk shim and GRUB as managed by `bootupd`. Skips live sessions (`/sysroot` on `erofs`/`squashfs`) and image variants without `bootupctl` installed, both without committing so a later switch retries. Tracked by #363. |
| `10-tailscale.sh` | Set the local user as the Tailscale operator. |
| `11-framework-ucsi-workaround.sh` | Append the `usbcore.autosuspend=-1` karg on Intel-Core-Ultra Frameworks. |
| `20-home-labels.sh` | Relabel `/var/home` once on systems installed before #261. |
| `99-flatpaks.sh` | Drop the Firefox system-config defaults at first boot. |

`05-bootupctl-adopt.sh` is the canonical example of a transient-skip body:
each guard (`command -v bootupctl`, the live-session check) exits without
committing so a later `bootc switch` that brings bootupctl in or moves off
the live root retries cleanly. `20-home-labels.sh` is the deliberate-skip
example: a `restorecon` that finds no work to do commits and stops
re-running.

## Tests

`tests/test_setup_hook_version_contract.py` asserts the contract on
`20-home-labels.sh`; `tests/test_bootupctl_adopt_hook.py` asserts the
contract on `05-bootupctl-adopt.sh` and additionally drives the real hook
against fake `libsetup.sh` and `bootupctl` stubs for the four state
transitions (fresh install, re-run after commit, live session, missing
`bootupctl`). `tests/test_ghostty_hook.py` follows the same shape for
`user-setup.hooks.d/30-ghostty.sh`, driving it against scratch homes for the
fresh, migrate, already-symlinked and reverse-symlink (`~/.config/ghostty`
pointing into the per-app dir) transitions, plus the two user-managed layouts
the hook must not disturb: the per-app path symlinked at a dotfiles directory,
and both paths symlinked at one. Run the suite with `just test`.
`just check` syntax-checks every hook (`bash -n`) through
`scripts/check-script-syntax.py`; there is no shellcheck gate in the Justfile
or CI.
