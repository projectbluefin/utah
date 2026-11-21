## sudo-rs and uutils-coreutils (#1229)

Dakota ships sudo-rs as `sudo` (`elements/bluefin/sudo-rs.bst`) and uutils as
coreutils (`elements/bluefin/uutils-coreutils.bst`). Both elements overwrite
another element's files. Utah does the same for sudo-rs, which replaces
`/usr/bin/sudo` in the image build. uutils is installed beside GNU coreutils,
not in its place (#1431).

### uutils stays beside GNU coreutils (#1431)

- **Why not the swap.** #1305 swapped `coreutils-single` for the factory's
  `uutils-coreutils-replace`, which links the GNU names to uutils. bootupd,
  which installs GRUB, copies the EFI tree onto the vfat ESP with `cp -a`
  (from the target image, inside `bootc install`). GNU cp does not diagnose
  xattrs vfat cannot hold; uutils cp fails with `Operation not supported (os
  error 95)` on `EFI/fedora/grubx64.efi`. Every fisherman install, so every
  LUKS E2E job, failed and `:testing` stopped advancing.
- **What ships.** `uutils-coreutils` alone (the multicall binary). No `[swap]`
  entry, no `uutils-coreutils-replace`, no `gnu-env`: `coreutils-single`
  owns the GNU names, `env` included.
- **Guards.** `configure-services.sh` fails the build if `/usr/bin/cp` links
  to uutils, and `tests/test_package_resolution.py` fails if
  `packages/utah.toml` swaps out `coreutils-single` again.
- **To retry the swap,** first get a uutils cp that skips unsupported xattrs
  under `-a` (or keep a GNU `cp`, as `gnu-env` keeps `env`), then run the LUKS
  E2E (`post-testing-e2e.yml`) on the candidate before merging. The `[swap]`
  mechanism in `install-packages.py` stays for that.

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

The contract asserts `uutils-coreutils`, `env`, `sudo-rs`, C sudo's PAM
stacks and `/etc/sudoers`. The local image build behind #1305 recorded the
checks below, with the swap #1431 later backed out; they did not cover a
`bootc install`.

- **Factory RPMs** (projectbluefin/utah-packages#1234, hermetic lane run
  44676259983). `uutils-coreutils-replace-0.7.0-9.hum1.bfin` owns 213 files:
  the 104 GNU program names, `[` included, and 109 `uutils-<prog>` names, all
  linked to `uutils-coreutils`; every name is in `uutils-coreutils --list`.
  It owns no `env`, `kill`, `uptime`, `hostname` or `more`, provides
  `coreutils = 9.12`, and requires `gnu-env` and the matching
  `uutils-coreutils`. `gnu-env-9.12-1.hum1.bfin` owns `/usr/bin/env` and
  conflicts with `coreutils(x86-64)` and `coreutils-single`. `rpm -U --test`
  of the unversioned first build hit util-linux's `coreutils < 8.20`
  conflict, hence the version.
- **Image build** (`just build-local` on #1305, against the c288398 factory
  repository plus those RPMs). One transaction installed the replacement and
  `gnu-env` and removed `coreutils-single`; all 398 contract packages
  verified. `configure-services.sh` passed its uutils, `gnu-env`, sudo-rs link
  and `sudo_exec_t` checks. The dracut initramfs rebuild passed its
  microcode, unlock-module, Plymouth and zstd checks on uutils, and
  `bootc container lint` passed 13 checks. `just check-desktop-contract`
  passed.
- **Finished image.** `sudo --version` is sudo-rs 0.2.15, `ls --version`
  uutils 0.7.0, `env --version` GNU 9.12, `sort` uutils. `rpm -q
  --whatprovides coreutils` names only the replacement; `coreutils-single` is
  gone; `rpm -V` of uutils-coreutils, the replacement and `gnu-env` is clean.
  `/usr/bin/sudo` is setuid and byte-identical to `sudo-rs`, `sudoedit`
  links to it, `su-rs` is not setuid. `rpm -V sudo` reports only
  `/usr/bin/sudo`, plus the `sudoers.d` mode and `/var/db/sudo` it reported
  before. `kill` and `more` stay util-linux's, `uptime` procps-ng's,
  `hostname` the hostname package's. `sudo -u nobody id -un`,
  `visudo-rs -c`, `ujust --list`, `uupd --help` and `dracut --help` work.
