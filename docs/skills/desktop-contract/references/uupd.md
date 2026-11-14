## uupd (Dakota parity)

Utah's update stack matches Dakota's `elements/bluefin/uupd.bst`:

- The Containerfile pins the uupd release Dakota uses (`UUPD_VERSION`, with
  digests for the tarball and both raw units).
- `configure-services.sh` installs upstream's `uupd.service`, adding
  `--disable-module-distrobox`, and upstream's daily 04:00 `uupd.timer`. Dakota
  writes the same units inline. The downloaded timer replaces Common's
  four-a-day copy. Dakota resolves that overlap the same way: BuildStream
  stages `bluefin/uupd.bst` after `bluefin/common.bst`.
- `system_files` ships Dakota's `uupd-manual.service` (static,
  `--hw-check=false`), `/etc/polkit-1/rules.d/uupd.rules` (local users may
  start `uupd.service` and `uupd-manual.service`) and `/etc/uupd/config.json`
  (hardware checks), which overrides Common's distrobox-only config.
- Common's `uupd-resume.timer`, `uupd-on-ac.service`, udev rule and
  `uupd.service.d/10-bluefin.conf` stay, as on Dakota.

`[configuration]` requires these files and pins the timer schedule, the
service flag and the hardware-check config, so the build fails if Common's
copies win. `test_dakota_uupd_files_shipped_and_required` in
`tests/test_desktop_contract.py` covers the contract entries.
