## Dakota system defaults

Utah copies these small system defaults from Dakota (`testing`) and asserts
each in `contracts/bluefin-desktop.toml` (`[configuration].files` plus
`file_contains`):

- `system_files/shared/usr/share/wireplumber/wireplumber.conf.d/51-disable-pcsp.conf`
  (Dakota `files/wireplumber/`, `elements/bluefin/wireplumber-pcsp.bst`) hides
  the PC-speaker ALSA card so it never becomes the default sink after resume.
- `system_files/shared/usr/lib/sysctl.d/60-kexec-disable.conf` (Dakota
  `elements/bluefin/kexec-disable.bst`) sets `kernel.kexec_load_disabled = 1`;
  bootc upgrades and soft-reboot do not use kexec, and Easy Anti-Cheat
  requires it off.
- Dakota's `files/polkit/org.freedesktop.NetworkManager.rules` (local wheel
  users modify system connections without a password) is already shipped,
  with an identical rule body, by Hummingbird's NetworkManager RPM at
  `/usr/share/polkit-1/rules.d/`. Utah asserts it in the contract and does not
  overlay a copy over the package-owned file.

## Lid and sleep

Lid and sleep handling stays at systemd-logind's defaults (suspend). Dakota
ships no logind or sleep override, and Utah's only swap is zram, so the
suspend-then-hibernate override inherited from bluefin-lts's `40-services.sh`
had no resume target and was removed from `scripts/configure-services.sh`.
