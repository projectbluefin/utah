## sudo-rs and uutils-coreutils (#1229)

Dakota ships sudo-rs as `sudo` (`elements/bluefin/sudo-rs.bst`) and uutils as
coreutils (`elements/bluefin/uutils-coreutils.bst`). Both elements overwrite
another element's files. Utah does the same: uutils replaces coreutils at the
RPM level, and sudo-rs replaces `/usr/bin/sudo` in the image build.

### uutils is coreutils

- **RPM swap.** The factory's `uutils-coreutils-replace` subpackage links GNU
  coreutils' program names in `/usr/bin` (the set `coreutils-single` ships,
  less `env`) to `uutils-coreutils`. It also adds Dakota's
  `uutils-<utility>` names. It provides `coreutils = 9.12`, the GNU release
  Hummingbird ships (unversioned, it would match util-linux's
  `Conflicts: coreutils < 8.20`), and conflicts with
  `coreutils-single`. `[swap]` in `packages/utah.toml` makes the package
  transaction `dnf5 do --action=install … --action=remove coreutils-single`,
  so everything that requires `coreutils` stays satisfied and the RPM database
  owns every path. `rpm -qf /usr/bin/ls` names the replacement.
- **Kept off uutils.** `kill` and `uptime` stay procps-ng's and util-linux's,
  `hostname` stays the hostname package's, and `more` stays util-linux's.
  Neither coreutils ships them. `env` stays GNU's: pkexec realpath()s its
  target, and the multicall binary dispatches on the executed path, so a
  symlinked `env` breaks `pkexec -u <user> /usr/bin/env <cmd>`, which uupd
  runs (Dakota's reason). The factory's `gnu-env` builds `env` alone from the
  GNU coreutils release Hummingbird ships, and the replacement requires it.
- **When it applies.** The swap is in the main package transaction, so
  everything after it runs on uutils: the remaining package scriptlets, the
  GNOME extension build, `configure-services.sh`, the dracut initramfs rebuild,
  SELinux labelling, `bootc container lint`, and every script on the booted
  system.
- `configure-services.sh` fails the build unless `/usr/bin/ls` links to
  uutils and `gnu-env` owns `/usr/bin/env`. `install-packages.py` fails it if
  `coreutils-single` survives the transaction.

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

The contract asserts `uutils-coreutils`, `uutils-ls`, `uu_ls`, `env`,
`sudo-rs`, C sudo's PAM stacks and `/etc/sudoers`. The local image build
behind #1305 recorded the checks below.
