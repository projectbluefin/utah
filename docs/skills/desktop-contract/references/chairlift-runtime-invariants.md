# ChairLift runtime invariants (#676)

The Control Center (ChairLift) ships from Common's pinned OCI artifact
(`COMMON_IMAGE_SHA`); Utah inherits the payload rather than re-staging a
release. The runtime invariants the in-image verifier cannot observe
directly are pinned here so a Common release that drops or rewrites the
relevant artifacts fails `just check` rather than the post-install E2E.

## Homebrew native compilation invariants

Homebrew's internal Bundler runs with a strict PATH filter
(`/usr/bin:/bin:/usr/sbin:/sbin`); a gem compile without host GNU `make`
bricks the persistent `/home/linuxbrew/.linuxbrew` prefix. The runtime
toolchain retention in the Bluefin contract and the script that prunes
the build closure must therefore hold:

- `packages/bluefin.toml` `[fedora]` retains `make`, `gcc`, `gcc-c++`.
  bluefin.toml is a byte-for-byte copy of projectbluefin/bluefin's
  `build_files/packages/base.toml` (the parity contract enforced by
  `just check-parity`); any drift here is a parity bug. `packages/utah.toml`
  `[parity]` / `[unavailable]` / `[build]` / `[excluded]` must not
  redefine any of these three names -- if a Utah-only install needs an
  explicit override, it belongs in `[parity]` with a comment, never by
  shadowing the contract.
- `scripts/configure-services.sh` removes the build-time extension
  toolchain (`dbus-devel`, `glib2-devel`, `meson`, `sassc`) before the
  image is shipped. The `make`/`gcc`/`gcc-c++` triple is not in the
  removal list; adding any of them breaks the Homebrew native compile
  path. The removal is followed by a `for pkg in ... ; do rpm -q "$pkg"`
  failure gate so an unexpected regression in the closure fails the
  build rather than the boot.

## /var/home symlink structure for the brew prefix

`/home` is a symlink to `/var/home` on Hummingbird, so both
`/home/linuxbrew/.linuxbrew` and `/var/home/linuxbrew/.linuxbrew`
resolve to the same inode. The contract and the system_files hooks must
agree on the spelling:

- `contracts/bluefin-desktop.toml` pins the `chairlift.desktop` `Exec=`
  as `/home/linuxbrew/.linuxbrew/bin/chairlift-wrapper` -- the brewed
  binary path Common's release ships.
- `system_files/shared/etc/profile.d/90-bluefin-starship.sh` checks
  `/var/home/linuxbrew/.linuxbrew/bin/starship` first (so a first login
  that runs before the symlink resolves still finds the brewed binary)
  and falls back to the system PATH.
- `system_files/shared/usr/share/ublue-os/user-setup.hooks.d/20-framework.sh`
  uses `BREW_PREFIX="/home/linuxbrew/.linuxbrew"`, the canonical
  spelling Common tooling depends on.
- The contract pins `/usr/bin/brew-preinstall`,
  `/usr/libexec/brew-preinstall` and
  `/usr/lib/systemd/user/brew-preinstall.service` so a Common release
  that splits or coalesces the binaries fails the in-image verifier.

## Polkit update-staging path

The privileged update path runs through
`/usr/libexec/bootc-update-stage`. ChairLift asks for it via
`/usr/share/polkit-1/actions/io.projectbluefin.chairlift.bootc.policy`,
and the contract pins the `<annotate
key="org.freedesktop.policykit.exec.path">/usr/libexec/bootc-update-stage</annotate>`
line. The annotation is what `pkexec` reads when escalating; a missing
or mistyped annotation silently escalates against an unrelated helper.

## Static-gate coverage

`tests/test_chairlift_setup.py` is the static-gate suite for the
invariants above:

- `ChairLiftContractAlignmentTests` reads the contract and asserts
  every release artifact Common ships is declared, with each
  `file_contains` line pinned to its literal payload (the trusted tap,
  the cask name, the chairlift-wrapper `Exec=` path, the bootc polkit
  annotation). The unit's enablement in the user preset and in
  `configure-services.sh` is checked alongside the contract's
  `user_enabled` list, with the global enable explicitly ordered after
  `ublue-user-setup.service` (which creates the user) and before
  `authselect enable-feature`.
- `BootcUpdateStagePolkitTests` pins the helper binary path and the
  polkit action's `<annotate>` line.
- `HomebrewToolchainRetentionTests` pins `make`/`gcc`/`gcc-c++` in
  `packages/bluefin.toml` `[fedora]` and rejects their removal in
  `configure-services.sh`, while keeping the build-time toolchain
  (`dbus-devel`, `glib2-devel`, `meson`, `sassc`) intact.
- `HomebrewSymlinkPathTests` pins both spellings of the brewed prefix
  across the contract and the system_files hooks.
- `DesktopContractTestsClean` runs
  `scripts/verify-desktop-contract.py --check` against the shipped
  contract.

The gate proves image composition. Runtime verification on a booted VM
(`systemctl --user status brew-preinstall`,
`brew list --cask chairlift`, launch Control Center, trigger an
authenticated system update) is still required to prove the GUI
downloads and authenticates correctly.
