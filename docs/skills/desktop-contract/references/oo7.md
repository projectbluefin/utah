## oo7 is the Secret Service (#1227)

Dakota uses GNOME OS's oo7 in place of gnome-keyring. GNOME OS builds it in
gnome-build-meta `core-deps/oo7-daemon.bst`, `oo7-pam.bst` and
`oo7-portal.bst`, and Dakota adds `bluefin/oo7-passwd-pam.bst`. Utah
installs the factory's `oo7-daemon`, `pam_oo7`, `oo7-portal` and `oo7-cli`
(utah-packages `packages/oo7`, a Rawhide import).

- **Packages.** `pam_oo7` and `oo7-daemon` obsolete `gnome-keyring-pam` and
  `gnome-keyring` below 51. `packages/base.toml` `[excluded]` removes both
  anyway. The factory's gdm requires `pam_oo7`, as Rawhide's does
  (utah-packages#1156). A gdm that still requires `gnome-keyring-pam` can't
  be installed next to oo7.
- **Login.** gdm 51's `gdm-password` and `gdm-autologin` stacks already call
  `-auth`, `-password` and `-session optional pam_oo7.so`, so Utah edits no
  gdm PAM file. The contract checks `gdm-password`.
- **Password changes.** shadow-utils' `/etc/pam.d/passwd` calls
  `-password optional pam_gnome_keyring.so use_authtok` after its
  `system-auth` substack. `configure-services.sh` points that line at
  `pam_oo7.so`, so `passwd` and Settings re-encrypt the login keyring, which
  is what Dakota's override does. The edit is guarded: already switched is a
  no-op, and any other shape fails the build.
- **Session.** GNOME OS links `oo7-daemon.service` into
  `gnome-session.target.wants`. Utah enables it globally: the user preset,
  `systemctl --global enable` in `configure-services.sh` (required, so the
  build fails if the unit is missing), and the contract's `user_enabled`.
- **Portal.** gnome-session 51's `gnome-portals.conf` already prefers
  `org.freedesktop.impl.portal.Secret=oo7-portal`, which the contract checks.
