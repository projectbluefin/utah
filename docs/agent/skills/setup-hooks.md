# First-boot setup

Inspect `systemctl status ublue-system-setup.service brew-setup.service`
and `systemctl --user status ublue-user-setup.service brew-preinstall.service`.
Read their current definitions with `systemctl cat` (or `--user cat`) and
journals with `journalctl -b -u UNIT` (or `--user -u UNIT`). First-login
Homebrew and Flatpak downloads need working networking; an enabled unit does
not prove its download finished.

Setup hooks and their once-only stamps are implementation details. Inspect
the unit and hook before considering a rerun; do not delete stamps blindly.
System state belongs under `/var`, administrator configuration under `/etc`,
and user configuration under the home directory. Do not edit shipped hooks
under `/usr`. Permanent hook changes require the Common or Utah checkout
identified in the source map and a rebuilt image.
