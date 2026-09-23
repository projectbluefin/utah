#!/usr/bin/bash
# Configure Utah's desktop services in the image, following bluefin-lts's
# build_scripts/40-services.sh. Hummingbird deliberately ships a server preset;
# these enablements are the part that turns the installed GNOME packages into a
# booting workstation.

set -eoux pipefail

# Match Bluefin's laptop defaults from 40-services.sh.
if [ -f /usr/lib/systemd/logind.conf ]; then
    sed -i 's/^#HandleLidSwitch=.*/HandleLidSwitch=suspend-then-hibernate/' /usr/lib/systemd/logind.conf
    sed -i 's/^#HandleLidSwitchDocked=.*/HandleLidSwitchDocked=suspend-then-hibernate/' /usr/lib/systemd/logind.conf
    sed -i 's/^#HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=suspend-then-hibernate/' /usr/lib/systemd/logind.conf
    sed -i 's/^#SleepOperation=.*/SleepOperation=suspend-then-hibernate/' /usr/lib/systemd/logind.conf
fi

unit_exists() {
    systemctl cat "$1" >/dev/null 2>&1
}

user_unit_exists() {
    for dir in /usr/lib/systemd/user /usr/local/lib/systemd/user /etc/systemd/user; do
        [ -e "$dir/$1" ] && return 0
    done
    return 1
}

enable_unit() {
    unit_exists "$1" && systemctl enable "$1" || true
}

disable_unit() {
    unit_exists "$1" && systemctl disable "$1" || true
}

# Services shared with Bluefin LTS. Optional units are guarded because
# Hummingbird intentionally does not ship every Bluefin integration package.
# Not enabled: rechunker-group-fix.service. It exists to repair the
# /usr/lib/{group,gshadow} damage left by the legacy rechunker
# (hhd-dev/rechunk) when rebasing to an image built without it. Utah has never
# been through legacy-rechunk -- so there is nothing here for it to repair.
# (Utah is not rechunked at all today: reusable-build skips chunkah on the
# testing stream, which is the only stream Utah builds. Tracked by #131.)
#
# It is not merely useless, it breaks the boot. The unit orders itself both
# After=local-fs.target and Before=systemd-sysusers.service, and local-fs
# already comes after sysusers via systemd-tmpfiles-setup-dev.service and
# local-fs-pre.target. systemd resolves the cycle by deleting a job from it --
# systemd-tmpfiles-setup-dev.service, which creates the static device nodes --
# and the installed system then times out every .device unit at once,
# /dev/ttyS0 as readily as the /boot filesystem, and lands in an emergency
# shell. Remove it outright: disabling only drops the preset's symlinks, and
# anything that later pulls the unit in by name would bring the cycle back.
# Strip the unit and its drop-in directory so nothing can order against it.
disable_unit rechunker-group-fix.service
rm -f /usr/lib/systemd/system/rechunker-group-fix.service
rm -rf /usr/lib/systemd/system/rechunker-group-fix.service.d
rm -f /usr/lib/systemd/system/*.wants/rechunker-group-fix.service \
      /etc/systemd/system/*.wants/rechunker-group-fix.service
enable_unit brew-setup.service
enable_unit flatpak-nuke-fedora.service
enable_unit flatpak-preinstall.service
# Bluetooth is firmware-less on the X230's BCM20702 (no BCM20702A1 blob is
# needed), but the service was never enabled, so no adapter appears even with
# bluez installed. Enable it next to the other desktop units; see #98.
enable_unit gdm.service
enable_unit bluetooth.service
enable_unit firewalld.service
enable_unit fwupd.service
enable_unit fwupd-refresh.timer
enable_unit dconf-update.service
enable_unit tailscaled.service
enable_unit uupd.timer
enable_unit ublue-system-setup.service
enable_unit systemd-resolved.service
enable_unit bootc-unified-storage.service
# input-remapper is installed by Bluefin's package set, but without its root
# daemon running, udev autoload fails on input devices and the GUI prompts for
# root credentials on launch. Enable it next to the desktop units; see #99.
enable_unit input-remapper.service

# Bluefin's Brewfile and Bazaar preinstall hook need the Flathub remote before
# first boot. Keep this as a .flatpakrepo descriptor so the remote is available
# to both flatpak-preinstall and brew-setup without baking mutable /var state.
install -d -m0755 /etc/flatpak/remotes.d
curl --fail --retry 3 --silent --show-error \
    --output /etc/flatpak/remotes.d/flathub.flatpakrepo \
    https://dl.flathub.org/repo/flathub.flatpakrepo

disable_unit flatpak-add-fedora-repos.service

# Keep image updates under uupd/bootc rather than the legacy rpm-ostree path.
# Mask in /usr/lib as well as /etc so the mask survives cross-vendor /etc 3-way merges
# when switching from systems that have bootc-fetch-apply-updates enabled (such as Bluefin).
# In a cross-vendor switch, ostree's 3-way /etc merge carries timers.target.wants/
# symlinks into /etc, which would re-enable the timer if /usr/lib was unmasked.
disable_unit rpm-ostree.service
systemctl mask bootc-fetch-apply-updates.timer bootc-fetch-apply-updates.service
ln -sf /dev/null /usr/lib/systemd/system/bootc-fetch-apply-updates.timer
ln -sf /dev/null /usr/lib/systemd/system/bootc-fetch-apply-updates.service
rm -f /usr/lib/systemd/system/*.wants/bootc-fetch-apply-updates.* \
      /etc/systemd/system/*.wants/bootc-fetch-apply-updates.*

# Hummingbird is a server base and declares the serial console on the kernel
# cmdline in /usr/lib/bootc/kargs.d/00-base.toml ("console=ttyS0,115200n8").
# systemd-getty-generator reads that argument and instantiates
# serial-getty@ttyS0.service for it, so a desktop machine with no serial port
# gets an agetty that dies on EIO roughly every ten seconds for the whole
# session: 197 journal entries in one boot on the X230, and a pointless wakeup
# each time on battery. Bluefin carries neither half and stays silent; see #103.
#
# The karg itself cannot be withdrawn from here. bootc's kargs.d is additive
# only, and dropping an argument a base image declared there is documented
# undefined behavior, so masking the unit is the lever the image actually has.
# Mask the instance, not serial-getty@.service: ttyS0 is the port the base
# names, and a genuinely attached serial device on another port still gets its
# login. A mask also outranks the generator's getty.target.wants symlink, which
# is created in /run at boot and so cannot be removed at build time.
#
# Both /etc and /usr/lib, for the same cross-vendor 3-way merge reason as the
# update timer above.
systemctl mask serial-getty@ttyS0.service
ln -sf /dev/null /usr/lib/systemd/system/serial-getty@ttyS0.service
rm -f /usr/lib/systemd/system/*.wants/serial-getty@ttyS0.service \
      /etc/systemd/system/*.wants/serial-getty@ttyS0.service

# SSH follows TunaOS's convention: closed in published images, opt-in for a
# local debug build. The preset must agree or first-boot preset-all will undo
# the build-time enablement.
if [[ "${ENABLE_SSHD:-0}" == "1" ]]; then
    enable_unit sshd.service
    sed -i 's/^disable sshd.service$/enable sshd.service/' \
        /usr/lib/systemd/system-preset/85-utah-desktop.preset
else
    disable_unit sshd.service
fi

# These are global user-service presets, so systemctl needs --global.
if user_unit_exists podman-auto-update.timer; then
    systemctl --global enable podman-auto-update.timer
fi
if user_unit_exists ublue-user-setup.service; then
    systemctl --global enable ublue-user-setup.service
fi

# Match Bluefin's login behavior. The operations are idempotent and authselect
# is present in the Hummingbird base.
authselect enable-feature with-silent-lastlog
authselect enable-feature with-fingerprint

# uupd is distributed as a release binary, not an RPM in the Hummingbird
# repositories. Keep its version pinned by the Containerfile argument.
install -Dm0755 /tmp/uupd/uupd /usr/bin/uupd
install -Dm0644 /tmp/uupd/uupd.service /usr/lib/systemd/system/uupd.service
install -Dm0644 /tmp/uupd/uupd.timer /usr/lib/systemd/system/uupd.timer
systemctl enable uupd.timer
# Avoid pulling the distrobox module on every update, as in Bluefin LTS.
sed -i 's|uupd|& --disable-module-distrobox|' /usr/lib/systemd/system/uupd.service

# Hummingbird's resolved unit defaults to a disconnected private /tmp. The
# Bluefin LTS workaround is needed for bootc's early-boot DNS path.
sed -i 's@^PrivateTmp=.*@PrivateTmp=no@' /usr/lib/systemd/system/systemd-resolved.service
rm -rf /tmp/uupd

# Build-only extension tooling is not part of the desktop image. unzip stays:
# it is in [parity] as well as [build], because Bluefin ships it to users.
DNF="$(command -v dnf5 || command -v dnf)"
"$DNF" -y remove --no-autoremove dbus-devel glib2-devel meson sassc

echo "Utah desktop service configuration complete"
