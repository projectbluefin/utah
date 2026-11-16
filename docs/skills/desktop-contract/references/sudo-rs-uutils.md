## sudo-rs and uutils-coreutils (#1229)

Dakota ships sudo-rs as `sudo` (`elements/bluefin/sudo-rs.bst`) and
uutils-coreutils (`elements/bluefin/uutils-coreutils.bst`). Utah installs the
factory's `sudo-rs` and `uutils-coreutils`, both Fedora Rawhide imports.

- **sudo-rs is `sudo`.** The factory package installs only
  `/usr/bin/sudo-rs`, `su-rs` and `visudo-rs`, and it requires C sudo. Neither
  package uses alternatives, and sudo-rs has no `Provides: sudo`. Dakota's
  compose stages C sudo first and lets sudo-rs overwrite `/usr/bin/sudo` and
  `sudoedit`. `configure-services.sh` instead makes both names symlinks to the
  setuid `sudo-rs`, which picks its mode from the name it was run as. C sudo
  stays installed for its `/etc/pam.d/sudo`, `sudo-i` and `/etc/sudoers`
  (with `sudoers.d`, including `dakota-config-sudo-linuxbrew-path`), as on
  Dakota. `su` stays util-linux's and `visudo` stays C sudo's.
  - `rpm -V sudo` reports the two replaced links. Nothing runs dnf on a booted
    image, and every image build reinstalls and relinks them.
  - The setuid binary is labelled `bin_t`, not `sudo_exec_t`. That changes
    nothing for the targeted policy's unconfined users, but a confined SELinux
    user (`staff_u`) would not transition to `sudo_t`.
- **uutils runs beside GNU coreutils.** Dakota overwrites the GNU names, except
  `kill`, `uptime` and `env`. Utah keeps GNU at `/usr/bin/<prog>`. Those files
  belong to Hummingbird's `coreutils-single` RPM, and the rest of the image
  build runs against them: package scriptlets, the dracut initramfs rebuild
  and SELinux labelling. Overwriting them would also leave the RPM database
  wrong about every coreutils path. uutils is reachable under Dakota's names,
  `/usr/bin/uutils-<prog>` (links `configure-services.sh` makes from
  `uutils-coreutils --list`), and under the RPM's own `uu_<prog>` links.
  The factory builds it with `feat_selinux`, so `-Z` works.

The contract asserts `sudo-rs`, C sudo's PAM file and sudoers,
`uutils-coreutils`, `uutils-ls` and `uu_ls`. The build itself fails if
`/usr/bin/sudo` doesn't end up pointing at `sudo-rs`.
