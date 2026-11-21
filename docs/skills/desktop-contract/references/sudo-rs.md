## sudo-rs (#1229)

Dakota ships sudo-rs as `sudo` (`elements/bluefin/sudo-rs.bst`) and uutils as
coreutils (`elements/bluefin/uutils-coreutils.bst`). Utah ships sudo-rs, which
replaces `/usr/bin/sudo` in the image build. It does not ship uutils.

### uutils comes from Homebrew

- **Not in the image.** uutils is available with
  `brew install uutils-coreutils`. The image keeps GNU coreutils
  (`coreutils-single`, `env` included) because bootupd and bootc rely on GNU
  cp semantics (#1433).
- **Why.** #1305 swapped `coreutils-single` for the factory's
  `uutils-coreutils-replace`, which links the GNU names to uutils. bootupd
  copies the EFI tree onto the vfat ESP with `cp -a` (from the target image,
  inside `bootc install`). GNU cp does not diagnose xattrs vfat cannot hold;
  uutils 0.7.0 cp failed with `Operation not supported (os error 95)` on
  `EFI/fedora/grubx64.efi`, so every install and every LUKS E2E job failed and
  `:testing` stopped advancing (#1431). #1433 backed the swap out; the
  multicall binary then sat unused beside GNU coreutils and was dropped too.
  The factory's `uutils-coreutils`, `uutils-coreutils-replace` and `gnu-env`
  recipes are unused by Utah.
- **Guards.** `configure-services.sh` fails the build if `/usr/bin/cp` links
  to uutils, and `tests/test_package_resolution.py` fails if
  `packages/utah.toml` swaps out `coreutils-single` or installs uutils.

### sudo-rs is sudo

- **C sudo stays.** sudo-rs reads C sudo's `/etc/pam.d/sudo`, `sudo-i` (the
  factory builds it with `pam-login`, for `sudo -i`), `/etc/sudoers` and
  `sudoers.d`, including Common's `001-bootc` and
  `dakota-config-sudo-linuxbrew-path`. The factory's `sudo-rs` requires
  `sudo`, and C sudo is a dnf-protected package, so it is not swapped out.
- **Image swap.** `configure-services.sh` hard-links `/usr/bin/sudo` to the
  setuid `/usr/bin/sudo-rs`. `/usr/bin/sudo` stays a setuid regular file,
  like Dakota's, and C sudo's own `sudoedit -> sudo` link now reaches sudo-rs,
  which dispatches on the name. `rpm -V sudo` reports `/usr/bin/sudo` changed;
  nothing else in the sudo package changes. `visudo` and `su` stay C sudo's
  and util-linux's, as on Dakota. The unused `su-rs` loses its setuid bit.
- **SELinux.** Fedora's policy labels `/usr/bin/sudo-rs` `sudo_exec_t`, the
  same type as `/usr/bin/sudo`, so the domain transition is C sudo's and no
  Utah policy module is needed. The build checks both paths with
  `matchpathcon` and fails if the label changes. `/run/sudo-rs`, sudo-rs's
  timestamp directory, is `var_run_t`, where C sudo's `/run/sudo` is
  `pam_var_run_t`; that only matters to confined users.
- **What sudo-rs doesn't do.** It has no `ROLE=`/`TYPE=`: a confined `staff_u`
  user whose sudoers rule uses them gets a parse error for that rule instead
  of `sysadm_r`. sudo-rs prints parse errors for unparseable sudoers lines on
  every run, and ignores the Fedora `Defaults` it doesn't implement. Check a
  migrated `/etc/sudoers.d` with `visudo-rs -c`.

### Evidence

The contract asserts `env`, `sudo-rs`, C sudo's PAM stacks and
`/etc/sudoers`. The local image build behind #1305 recorded the sudo-rs
checks: `configure-services.sh` passed its sudo-rs link and `sudo_exec_t`
checks, and in the finished image `sudo --version` is sudo-rs 0.2.15,
`/usr/bin/sudo` is setuid and byte-identical to `sudo-rs`, `sudoedit` links
to it, `su-rs` is not setuid, and `rpm -V sudo` reports only `/usr/bin/sudo`,
plus the `sudoers.d` mode and `/var/db/sudo` it reported before.
`sudo -u nobody id -un`, `visudo-rs -c`, `ujust --list` and `uupd --help`
work.
