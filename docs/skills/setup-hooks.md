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

`projectbluefin/common#1196` adds a read-only check and a separate commit while
retaining `version-script` as the legacy check-and-record helper. The legacy
gate records the version *before* the body runs, so a failed body is skipped
forever after. The new pair records only successful completion and therefore
allows failed bodies to retry on the next boot.

Two consequences to keep in mind when writing the body:

1. **Use `set -e`.** Without it the hook runs on past a failed body and commits
   a completion that never happened.
2. **Split skip paths by whether the condition can change.** A deliberate skip
   (wrong vendor, karg already applied) commits before `exit 0` so the hook
   stops re-running every boot. A transient skip (DMI unreadable, dependency
   missing) exits *without* committing so it retries.
3. **Evaluate transient guards before `version-script-check`.** Under the compat
   shim the check *is* the legacy stamp, so a transient skip placed after it is
   recorded as done and never retries. Deliberate skips stay after the check,
   since they need the gate to have run before committing.

## Compat shim

Each migrated hook defines a shim when its sourced libsetup lacks the pair,
so a common image bump is not a merge-order prerequisite. The fallback keeps
legacy stamp-on-check behavior; it cannot provide retry-after-body-failure.

```bash
if ! declare -F version-script-check >/dev/null; then
    version-script-check() { version-script "$@"; }
    version-script-commit() { :; }
fi
```

Utah's migrated privileged and user hooks carry this shim alongside their
check/commit calls. Deliberate Framework skips (wrong vendor/product, karg
already present) commit; unavailable DMI, missing rpm-ostree/brew and an
unwritable Homebrew prefix remain transient. The prefix warning can recur for
a non-admin account; that is intentional so a later permission or dependency
change gets a retry.

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
| `10-tailscale.sh` | Set a non-root pkexec caller as the Tailscale operator. Missing Tailscale or an invalid/root caller defers without stamping; a failed grant retries with the read-only API. |
| `11-framework-ucsi-workaround.sh` | Append the `usbcore.autosuspend=-1` karg on Intel-Core-Ultra Frameworks. Wrong hardware or an already-applied karg commits a deliberate skip; missing DMI or rpm-ostree retries. |
| `20-home-labels.sh` | Relabel `/var/home` once on systems installed before #261, repairing a mis-keyed active `file_contexts.homedirs` first (#474). |
| `99-flatpaks.sh` | Remove stale Bluefin Firefox preferences and copy optional defaults. Version 2 reruns machines that stamped version 1 while the quoted removal glob was a no-op (#489). Successful copies and deliberate absence/architecture skips commit; body failures retry with the read-only API. |

`05-bootupctl-adopt.sh` is the canonical example of a transient-skip body:
each guard (`command -v bootupctl`, the live-session check) exits without
committing so a later `bootc switch` that brings bootupctl in or moves off
the live root retries cleanly. `20-home-labels.sh` commits only after it
verifies the real on-disk label, not merely that `restorecon` exited zero
(#474): `file_contexts.subs_dist` aliases `/var/home` to `/home`, so a rule
keyed on the wrong root is unreachable and `restorecon` silently relabels
nothing. The hook detects that condition by comparing the active
`file_contexts.homedirs` against the image's own `/usr/etc` default,
reinstalls the pristine copy when they disagree on the home root, and only
then trusts a post-`restorecon` `stat` of `/var/home`.

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
and both paths symlinked at one. `tests/test_flatpaks_hook.py` executes real
preference cleanup and copying in scratch roots; the migrated-hook suite also
starts from a version-1 stamp and proves stale defaults are removed, user
preferences survive, and version 2 commits. Run the suite with `just test`.
`just check` syntax-checks every hook (`bash -n`) through
`scripts/check-script-syntax.py`; there is no shellcheck gate in the Justfile
or CI.

`tests/test_migrated_setup_hook_contract.py` executes all four migrated hooks
against both library contracts using scratch filesystem roots. It proves
successful bodies run once, body failures retry with the new pair, Framework
deliberate skips stamp and transient skips recover. It uses real Firefox copy
operations against scratch destinations, never host `/var/lib/flatpak`.
