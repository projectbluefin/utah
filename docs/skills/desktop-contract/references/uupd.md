## uupd (Dakota parity)

Utah's update stack matches Dakota's `elements/bluefin/uupd.bst`:

- The Containerfile pins the uupd release Dakota uses (`UUPD_VERSION`, with
  the tarball digest) and installs only the binary.
- The factory's `dakota-config-uupd` (#1265) installs Dakota's units as the
  element writes them: `uupd.service` with `--disable-module-distrobox`, the
  daily 04:00 `uupd.timer`, `uupd-manual.service` (static,
  `--hw-check=false`), `/etc/polkit-1/rules.d/uupd.rules` (local users may
  start `uupd.service` and `uupd-manual.service`) and `/etc/uupd/config.json`
  (hardware checks). The package's files replace Common's four-a-day timer
  and distrobox-only config, which are copied in before the package
  transaction; Dakota resolves the same overlap by staging `bluefin/uupd.bst`
  after `bluefin/common.bst`.
- Common's `uupd-resume.timer`, `uupd-on-ac.service`, udev rule and
  `uupd.service.d/10-bluefin.conf` stay, as on Dakota.

`[configuration]` requires these files and pins the timer schedule, the
service flag and the hardware-check config, so the build fails if Common's
copies win. `test_dakota_uupd_files_come_from_the_factory_and_are_required` in
`tests/test_desktop_contract.py` covers the contract entries.
