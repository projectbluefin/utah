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
- More upstream preset units Dakota runs (#1271, #1272), enabled the same
  three ways: nvme-cli's `nvmf-autoconnect.service` and
  `nvmefc-boot-connections.service` (each skipped by its own
  `ConditionPathExists=` unless NVMe-oF config or FC-NVMe hardware is
  present), `systemd-imdsd.socket`, the system `speech-dispatcherd.service`,
  `podman.service`, and netavark's `netavark-dhcp-proxy.socket`,
  `netavark-dhcp-proxy.service`, `netavark-firewalld-reload.service` and
  `netavark-nftables-reload.service`.
  `systemd-homed-firstboot.service` goes with #1238.
- mdadm's `mdcheck_start.timer`, `mdcheck_continue.timer` and
  `mdmonitor-oneshot.timer` stay off, in `[services].disabled`, although
  Dakota runs them. Utah keeps Fedora's `raid-check.timer` (#1268), and the
  mdcheck pair would schedule a second scrub of the same arrays. All three are
  `WantedBy=mdmonitor.service`. Fedora's preset starts that service on every
  boot, while Dakota starts it only when udev assembles an array. So the
  oneshot reminder would run daily everywhere, and
  `mdadm --monitor --oneshot --scan` exits 1 when `mdadm.conf` has no alert
  address.
- `nfs-client.target` is enabled, as Dakota's `elements/bluefin/nfs-utils.bst`
  presets it (#1269). Dakota builds nfs-utils `--disable-gss`, but Utah's Fedora
  build pulls in `gssproxy.service` (through `auth-rpcgss-module.service`, whose
  `Wants=` applies even without a keytab). The service's
  `StateDirectory=gssproxy/clients gssproxy/rcache` failed with
  `238/STATE_DIRECTORY`: clean-stage strips `/var`, and SELinux gives systemd
  (init_t) no `add_name` on `gssproxy_var_lib_t`, so systemd can't create the
  subdirectories. `usr/lib/tmpfiles.d/utah-gssproxy.conf` recreates all three
  directories with the RPM's modes, and systemd-tmpfiles is allowed to.

`ServiceMaskParityTests` in `tests/test_desktop_contract.py` pins the
three-way agreement for each group.
