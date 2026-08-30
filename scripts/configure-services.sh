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
enable_unit rechunker-group-fix.service
enable_unit brew-setup.service
enable_unit gdm.service
enable_unit firewalld.service
enable_unit fwupd.service
enable_unit fwupd-refresh.timer
enable_unit dconf-update.service
enable_unit tailscaled.service
enable_unit uupd.timer
enable_unit ublue-system-setup.service
enable_unit systemd-resolved.service
enable_unit bootc-unified-storage.service

# Keep image updates under uupd/bootc rather than the legacy rpm-ostree path.
disable_unit rpm-ostree.service
systemctl mask bootc-fetch-apply-updates.timer bootc-fetch-apply-updates.service

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

# Build-only extension tooling is not part of the desktop image.
DNF="$(command -v dnf5 || command -v dnf)"
"$DNF" -y remove --no-autoremove cmake dbus-devel glib2-devel meson sassc unzip

echo "Utah desktop service configuration complete"
