## Dakota service presets

Utah follows projectbluefin/dakota (`testing`) and GNOME OS: a unit with an
`[Install]` section is enabled unless a preset turns it off (#1233).

### How the policy is built

systemd reads preset files from `/etc`, `/run` and `/usr/lib` in lexical
order of their names; the first line that matches a unit wins.

| File | Owner | Role |
|---|---|---|
| `00-`…`03-*.preset` | brew, uupd, common | vendor enables |
| `80-hummingbird.preset` | Hummingbird | `disable dnf-makecache.timer`, `enable bootloader-update.service` |
| `85-utah-desktop.preset` | Utah | overrides the base's 80/90 files: `enable systemd-boot-update.service`; `disable` sshd, multipathd, lm_sensors, Dakota's iSCSI set (plus Fedora's `iscsi-starter`/`iscsi-onboot`), the serial getty, `getty@` (kmscon owns the VTs), rpm-ostree and bootc-fetch-apply-updates |
| `90-default.preset`, `90-systemd.preset` | Hummingbird | Fedora's server enables and disables, upstream systemd's list |
| `98-utah-enable-default.preset` | Utah | GNOME OS's and Dakota's disables, Utah's own disables, then `enable *` |
| `99-default-disable.preset` | Hummingbird | `disable *`; never reached |

A disable for a unit that Hummingbird's `90-default.preset` enables only works
in `85-utah-desktop.preset`. Everything else goes in `98-utah-enable-default.preset`.

`scripts/configure-services.sh` runs `systemctl preset-all` after the last unit
file lands, so the image carries the state first-boot preset application gives
it, and the in-image contract verifier checks that state. The script enables no
system unit by hand. Template units (`foo@.service`) without `DefaultInstance=`
are never enabled by a preset.

### What 98 disables

- GNOME OS (gnome-build-meta `files/systemd-presets/system-preset/80-network.preset`,
  `openssh.preset`, `wpa-supplicant.preset`): systemd-networkd and its socket
  and wait-online, the sshd socket and key helpers, the `wpa_supplicant@` templates.
- Dakota's element presets: `50-sysupdate` (plus `systemd-sysupdate.*`; Utah
  updates through bootc and uupd), `80-kanata`, `80-kmscon` (the service; its
  `getty@.service` line is in 85, which outranks Hummingbird's 90-systemd),
  `80-libvirt`, `80-nethogs`,
  `80-nfs-client` (server side), `80-pam-pkcs11`, `80-pcrlock` (verbatim
  order: machine-id and Secure Boot authority locked), `80-rpcbind`,
  `80-sysstat` (sar/iostat on demand; `systemctl enable --now sysstat.service`).
- Utah-only units 99-default-disable used to keep off: `authselect-first-boot`
  (would re-select `@system-default` on first boot and drop the build's
  authselect features) and `authselect-apply-changes`, `dbus-daemon` (would
  claim dbus-broker's alias), `livesys*`, `rpm-ostree*`,
  `dnf5-offline-transaction`, `flatpak-add-fedora-repos`, network servers
  (autofs, dnsmasq, httpd, openvpn, saned, ipp-usb, samba, wsdd, cups-browsed,
  gssproxy, nftables beside firewalld), CNI, and helpers that
  need configuration or change the system unasked (brltty, fancontrol,
  powertop, xfs_healer, canberra sounds, lastlog2 import, man-db cache).
- mdadm's `mdcheck_start.timer`, `mdcheck_continue.timer` and
  `mdmonitor-oneshot.timer`, although Dakota runs them (#1271). Utah keeps
  Fedora's `raid-check.timer` (#1268), and the mdcheck pair would schedule a
  second scrub of the same arrays. All three are `WantedBy=mdmonitor.service`,
  which Hummingbird's `90-default.preset` starts on every boot (Dakota starts
  it only when udev assembles an array), so the oneshot reminder would run
  daily everywhere, and `mdadm --monitor --oneshot --scan` exits 1 when
  `mdadm.conf` has no alert address.

### What enable-by-default turned on

Measured by resolving the 2026-11-14 testing image's unit list against these
presets and by `systemctl preset-all` in the Hummingbird base. Newly enabled, all of
them enabled on a booted Dakota: rtkit, upower, udisks2, accounts-daemon,
cups, pcscd.socket, gnome-remote-desktop(+configuration), malcontent's four
units, systemd-oomd, wpa_supplicant, podman (service, socket, auto-update,
restart, clean-transient), netavark's dhcp proxy and firewall reload units,
speech-dispatcherd, NVMe-oF autoconnect and FC boot connections (each gated
on NVMe-oF config or FC hardware by its own `Condition`), the
`systemd-*` credential, storage, sysext, factory-reset, pcrextend and imds
sockets, and Dakota's three pcrlock units. Utah-only extras left on because
they are harmless: `ratbagd.service` (D-Bus alias), `selinux-check-proper-disable`,
`unbound-anchor.timer`, `bootc-publish-rhsm-facts` (conditioned on rhsm).

### Units the contract asserts

`[services].enabled` asserts, in the built image: `avahi-daemon.service`,
`avahi-daemon.socket`, `bluetooth.service`, `ublue-firstboot-date.path`,
`ublue-booted-image.service`, `bluefin-remove-installer.service`
(dakota-config-firstboot-date, #1265), `ublue-wallpaper-month.service`,
`bluefin-wallpaper-month.service` (#1266), `bootc-unified-storage.timer`,
`brew-setup.service`, `cups.socket`,
`dconf-update.service`, `flatpak-nuke-fedora.service`,
`flatpak-preinstall.service`, `gdm.service`, `colord.service`,
`accounts-daemon.service`, `ModemManager.service`,
`power-profiles-daemon.service`, `rtkit-daemon.service`, `uresourced.service`,
`upower.service`, `udisks2.service`, `polkit-agent-helper.socket`,
`cups.service`, `pcscd.socket`, `gnome-remote-desktop.service`,
`gnome-remote-desktop-configuration.service`, `malcontent-timerd.service`,
`malcontent-timer-extension-agent.service`, `malcontent-webd.service`,
`malcontent-webd-update.timer`, `systemd-oomd.service`, `systemd-oomd.socket`,
`wpa_supplicant.service`, `podman.socket`, `podman-auto-update.timer`,
`podman-restart.service`, `podman-clean-transient.service`,
`systemd-sysext.socket`, `systemd-factory-reset.socket`,
`systemd-pcrextend.socket`, `systemd-storage-block.socket`,
`systemd-storage-fs.socket`, `systemd-udev-load-credentials.service`,
`systemd-userdb-load-credentials.service`, `thermald.service`,
`projectbluefin-countme.timer`, `switcheroo-control.service`,
`ublue-system-setup.service`, `uupd.timer`, `tailscaled.service`,
`firewalld.service`, `fwupd-refresh.timer`, `systemd-boot-update.service`,
`systemd-timesyncd.service` (#1228; chrony is not installed),
`systemd-pcrlock-make-policy.service`, `systemd-pcrlock-machine-id.service`,
`systemd-pcrlock-secureboot-authority.service`, `podman.service`,
`netavark-dhcp-proxy.socket`, `netavark-dhcp-proxy.service`,
`netavark-firewalld-reload.service`, `netavark-nftables-reload.service`,
`nvmf-autoconnect.service`, `nvmefc-boot-connections.service`,
`systemd-imdsd.socket`, `speech-dispatcherd.service` (#1271, #1272),
`bluefin-migrate-var-home-passwd.service`
(Dakota's legacy `/var/home` passwd migration for the `/home` bind mount,
#1234), `vmtoolsd.service` and `qemu-guest-agent.service` (#1232),
and `nfs-client.target` (#1269).
`[services].disabled` holds the network servers, sysstat, libvirt's sockets,
mdadm's timers, `systemd-homed-firstboot.service`, and the units
enable-by-default must not reach.

### systemd-homed (#1238): not adopted

Dakota's freedesktop-sdk PAM stack carries `-pam_systemd_home`; Utah does not
follow it (owner decision on #1238). Utah needs a clean migration path for
Fedora users, and GNOME OS has not moved to homed either. authselect's
`with-systemd-homed` stays off, and `98-utah-enable-default.preset` disables
`systemd-homed-firstboot.service`: with no user on a fresh install, `homectl
firstboot` would prompt on the console before GDM, ahead of
gnome-initial-setup.

### NFS client (#1269)

`nfs-client.target` is enabled, as Dakota's `elements/bluefin/nfs-utils.bst`
presets it: Hummingbird's `90-default.preset` enables it, and
`85-utah-desktop.preset` no longer disables it. Dakota builds nfs-utils `--disable-gss`, but Utah's Fedora
build pulls in `gssproxy.service` (through `auth-rpcgss-module.service`, whose
`Wants=` applies even without a keytab). The service's
`StateDirectory=gssproxy/clients gssproxy/rcache` failed with
`238/STATE_DIRECTORY`: clean-stage strips `/var`, and SELinux gives systemd
(init_t) no `add_name` on `gssproxy_var_lib_t`, so systemd can't create the
subdirectories. `usr/lib/tmpfiles.d/utah-gssproxy.conf` recreates all three
directories with the RPM's modes, and systemd-tmpfiles is allowed to.

### Virtualization (#1232)

Toolbox, systemd-container and the guest agents (qemu-guest-agent,
spice-vdagent, open-vm-tools, hyperv-daemons) are installed, as on Dakota.
Dakota's presets need no file of their own here: `80-libvirt.preset`'s
disables are in `98-utah-enable-default.preset`, and the enables in its
`80-open-vm-tools.preset` and `80-hyperv-daemons.preset` (`vmtoolsd`,
`hypervfcopyd`) follow from `enable *`; each agent is gated on its hypervisor.
`ujust setup-virtualization` and `manage-virtualization` (`60-custom.just`)
run Dakota's `files/just-overrides/devmode.py`, shipped verbatim as
`/usr/libexec/dakota-devmode`. qemu-kvm, virtiofsd and libvirt wait on the
factory (`[unavailable]`, utah-packages#1021: ipxe-roms-qemu and SDL2_image are
missing, libvirt is unpublished); until libvirt lands the recipes say so and
exit. containerd and runc are excluded: podman runs on crun.

### Key remapping (#1222)

Key remapping is kanata plus ydotool, as on Dakota, and input-remapper is
gone. The factory's `kanata` ships Dakota's `kanata.service` (DynamicUser,
`input` group, `/dev/uinput`) and its `80-kanata.preset`
(`disable kanata.service`); `85-utah-desktop.preset` and the contract's
`disabled` list keep it off as well. The unit has
`ConditionPathExists=/etc/kanata/kanata.kbd`, so opt in by writing that file
and running `systemctl enable --now kanata`. `ydotool` brings Dakota's
`80-ydotool.rules` (uinput to `input`, uaccess) and the `uinput` modules-load
drop-in (utah-packages#1134).

### iSCSI initiator (#1360)

`iscsi-initiator-utils` (with `-iscsiuio`) and `isns-utils` are installed, as
Dakota ships open-iscsi and open-isns, and every unit they ship stays off.
`85-utah-desktop.preset` carries Dakota's `50-iscsi.preset` (gnome-build-meta
`files/gnomeos/iscsi-config/`) verbatim, plus `iscsi-starter.service` and
`iscsi-onboot.service`: Fedora's package adds both, and Hummingbird's
`90-default.preset` enables them. `iscsi-shutdown.service` has no `[Install]`
section. The packages' NetworkManager dispatcher script only reloads
`iscsi.service`, which is a no-op while the unit is inactive. clean-stage strips
`/var`, so `tmpfiles.d/utah-iscsi.conf` recreates `/var/lib/iscsi` (with its
node database directories) and `/var/lib/isns` with the RPMs' modes and their
policy labels. To use iSCSI, run
`systemctl enable --now iscsid.socket iscsi.service`. The contract's `disabled`
list asserts eight of the nine units. It leaves out `iscsi.service`, because
Fedora's `[Install]` section for it holds only `Also=iscsi-starter.service`.
`systemctl is-enabled` therefore reports `indirect` whatever the preset says,
and `iscsi-starter.service`, which starts it at boot, is asserted instead.

### Console (#1237)

The VT consoles are kmscon, as on GNOME OS and Dakota, set up as
gnome-build-meta `gnomeos-deps/kmscon.bst` does. The factory's `kmscon` ships
no preset, so `98-utah-enable-default.preset` disables `kmscon.service` and
`85-utah-desktop.preset` disables `getty@.service` ahead of Hummingbird's
`90-systemd.preset`. `kmsconvt@.service` is on through enable-by-default: it
has `DefaultInstance=tty1` and `Alias=autovt@.service`, so logind spawns it on
every VT, and its `OnFailure=getty@%i.service` keeps a login if kmscon fails.
`/etc/kmscon/kmscon.conf` sets `font-name=Adwaita Mono` (drawn by
`kmscon-pango`), and `gdm.service.d/kmscon.conf` sets
`Conflicts=`/`After=kmsconvt@tty1.service` so GDM takes tty1 over. The
serial `serial-getty@ttyS0` mask is unchanged. The contract asserts the files,
`kmsconvt@.service` enabled, and `getty@`/`kmscon.service` disabled.

### User units

The user side keeps a catch-all disable, as GNOME OS does (gnome-build-meta
`user-preset/99-default-disable.preset`). `user-preset/85-utah-desktop.preset`
and the `configure-services.sh` user loop enable PipeWire, WirePlumber,
xdg-user-dirs, obex, mpris-proxy, `speech-dispatcher.socket` and
`xdg-desktop-portal-rewrite-launchers.service` (gnome-build-meta's
`80-speechd.preset` and `80-xdg.preset`); `[services].user_enabled` asserts them.

### Syncthing and Sysprof (#1361)

`syncthing` is installed from the factory, as Dakota ships it. It is a
background daemon, so Flatpak-first does not apply. Its `syncthing.service`
user unit stays off: the catch-all disable covers it, and
`[services].user_disabled` asserts that no user gets it unasked (opt in with
`systemctl --user enable --now syncthing`). The `syncthing@.service` system
template has no default instance. `configure-services.sh` removes the RPM's
"Start Syncthing" and "Syncthing Web UI" launchers, which Dakota does not
ship. bling's Sync Folder toggle runs its own Syncthing container and
migrates a native setup. Sysprof's app is the Flathub `org.gnome.Sysprof`,
which `ujust install-dev-flatpaks` offers. The host daemon (`sysprof-cli`:
`sysprofd`, `sysprof3.service`) waits on utah-packages#1239, because
Hummingbird's `sysprof-cli` requires `kernel-tools` and no enabled repository
publishes it.

### Re-measuring

Run `systemctl --root=/ preset-all` inside both images and diff
`systemctl --root=/ list-unit-files --state=enabled,disabled`. A unit Dakota
leaves on needs nothing; a unit that must stay off needs a `disable` line in
the right file and an entry in `[services].disabled`.

`ServiceMaskParityTests` in `tests/test_desktop_contract.py` resolves every
contract unit against the preset files the way systemd does.
