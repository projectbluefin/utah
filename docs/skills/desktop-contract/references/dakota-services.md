## Dakota service presets

Utah follows projectbluefin/dakota (`testing`) for these units. Each one is
wired in three places that must agree: `85-utah-desktop.preset` (system or
user), `scripts/configure-services.sh`, and `contracts/bluefin-desktop.toml`
`[services]`.

- `colord.service` and `accounts-daemon.service` are enabled. Dakota inherits
  them from gnome-build-meta's `files/systemd-presets/system-preset/80-gnome.preset`.
  They are in `[services].enabled`.
- `speech-dispatcher.socket` and `xdg-desktop-portal-rewrite-launchers.service`
  are globally enabled user units, from gnome-build-meta's
  `user-preset/80-speechd.preset` and `80-xdg.preset`. They are in the
  `configure-services.sh` user loop (guarded by `user_unit_exists`) and in
  `[services].user_enabled`.
- sysstat collection is opt-in. Dakota's `elements/bluefin/sysstat.bst`
  presets `sysstat.service`, `sysstat-collect.timer`, `sysstat-summary.timer`
  and `sysstat-rotate.timer` off; Utah's preset disables all four,
  `configure-services.sh` runs `disable_unit` on them, and the contract's
  `disabled` list holds them off. The package still ships, so `sar` and
  `iostat` work on demand; enable collection with
  `systemctl enable --now sysstat.service`.
- Key remapping is kanata plus ydotool, as on Dakota (#1222), and
  input-remapper is gone. The factory's `kanata` ships Dakota's
  `kanata.service` (DynamicUser, `input` group, `/dev/uinput`) and its
  `80-kanata.preset` (`disable kanata.service`). Utah's preset,
  `configure-services.sh` and the contract's `disabled` list keep it off as
  well. The unit has `ConditionPathExists=/etc/kanata/kanata.kbd`, so opt in
  by writing that file and running `systemctl enable --now kanata`. `ydotool`
  brings Dakota's `80-ydotool.rules` (uinput to `input`, uaccess) and the
  `uinput` modules-load drop-in (utah-packages#1134).

- Dakota's effective service state (#1233). Dakota has no system `disable *`
  preset, so a booted Dakota runs every installed unit with `[Install]` that
  no preset turns off. These units ship in Utah and are enabled on Dakota after
  `preset-all` (measured from `ghcr.io/projectbluefin/dakota:testing`). They
  are enabled in one block of the preset, in `configure-services.sh` and in
  `[services].enabled`:
  `rtkit-daemon.service`,
  `uresourced.service`,
  `upower.service`,
  `udisks2.service`,
  `polkit-agent-helper.socket`,
  `cups.service`,
  `pcscd.socket`,
  `gnome-remote-desktop.service`,
  `gnome-remote-desktop-configuration.service`,
  `malcontent-timerd.service`,
  `malcontent-timer-extension-agent.service`,
  `malcontent-webd.service`,
  `malcontent-webd-update.timer`,
  `systemd-oomd.service`,
  `systemd-oomd.socket`,
  `wpa_supplicant.service`,
  `podman.socket`,
  `podman-auto-update.timer`,
  `podman-restart.service`,
  `podman-clean-transient.service`,
  `systemd-sysext.socket`,
  `systemd-factory-reset.socket`,
  `systemd-pcrextend.socket`,
  `systemd-storage-block.socket`,
  `systemd-storage-fs.socket`,
  `systemd-udev-load-credentials.service`,
  `systemd-userdb-load-credentials.service`.
  `multipathd.service` and `lm_sensors.service`, which Dakota doesn't run,
  are in `[services].disabled`. To re-measure, run `systemctl --root=/
  preset-all` inside both images and diff `systemctl --root=/
  list-unit-files`.

`ServiceMaskParityTests` in `tests/test_desktop_contract.py` pins the
three-way agreement for each group.
