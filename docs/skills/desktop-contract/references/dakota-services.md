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

`ServiceMaskParityTests` in `tests/test_desktop_contract.py` pins the
three-way agreement for each group.
