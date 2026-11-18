## sudo-rs and uutils-coreutils (#1229)

Dakota ships sudo-rs as `sudo` (`elements/bluefin/sudo-rs.bst`) and
uutils-coreutils (`elements/bluefin/uutils-coreutils.bst`). Utah installs the
factory's `uutils-coreutils` now. sudo-rs is deferred.

- **uutils runs beside GNU coreutils.** Dakota overwrites the GNU names, except
  `kill`, `uptime` and `env`. Utah keeps GNU at `/usr/bin/<prog>`. Those files
  belong to Hummingbird's `coreutils-single` RPM, and the rest of the image
  build runs against them: package scriptlets, the dracut initramfs rebuild
  and SELinux labelling. Overwriting them would also leave the RPM database
  wrong about every coreutils path. uutils is reachable under Dakota's names,
  `/usr/bin/uutils-<prog>` (links `configure-services.sh` makes from
  `uutils-coreutils --list`), and under the RPM's own `uu_<prog>` links.
  The factory builds it with `feat_selinux`, so `-Z` works. Nothing changes
  for scripts that call the GNU names.
- **sudo stays C sudo.** Making sudo-rs `/usr/bin/sudo` waits on three things:
  1. A booted image where `sudo`, `sudo -i` and `sudoedit` work through
     `/etc/pam.d/sudo` and `sudo-i`.
  2. Confined SELinux users. sudo-rs has no `ROLE=`/`TYPE=` support, and its
     setuid binary is labelled `bin_t`, not `sudo_exec_t`, so a `staff_u` user
     never reaches `sysadm_r`. Fedora supports confined users, so a Fedora
     user who set that up would lose admin access.
  3. A migration check. A Fedora or Bluefin user who rebases keeps their
     `/etc/sudoers.d`. sudo-rs ignores directives it does not support and
     drops rules it cannot parse, without telling the user. Utah needs a check
     that reports those lines before the switch.
  When sudo-rs does land, drop the setuid bit from `su-rs`: nothing uses it.

The contract asserts `uutils-coreutils`, `uutils-ls` and `uu_ls`.
