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

# Under `set -e` above, a missing required unit aborts this script and fails
# the image build for every flavor -- not just this one. Only mark a unit
# required here if losing it silently would be worse than a broken build.
enable_required_unit() {
    if ! unit_exists "$1"; then
        echo "Required desktop unit is missing: $1" >&2
        return 1
    fi
    systemctl enable "$1"
}

enable_optional_unit() {
    if unit_exists "$1"; then
        systemctl enable "$1"
    else
        echo "Optional desktop unit is unavailable; leaving it disabled: $1"
    fi
}

disable_unit() {
    unit_exists "$1" && systemctl disable "$1" || true
}

# Services shared with Bluefin LTS. Optional units are guarded because
# Hummingbird intentionally does not ship every Bluefin integration package.
enable_optional_unit rechunker-group-fix.service
enable_required_unit brew-setup.service
enable_required_unit flatpak-nuke-fedora.service
enable_required_unit flatpak-preinstall.service
enable_required_unit gdm.service
enable_required_unit firewalld.service
enable_required_unit fwupd.service
enable_required_unit fwupd-refresh.timer
enable_required_unit dconf-update.service
enable_optional_unit tailscaled.service
enable_required_unit ublue-system-setup.service
enable_required_unit systemd-resolved.service
enable_required_unit bootc-unified-storage.service
enable_required_unit input-remapper.service
enable_required_unit bluefin-stats-refresh.timer

# Bluefin's Brewfile and Bazaar preinstall hook need the Flathub remote before
# first boot. Keep this as a .flatpakrepo descriptor so the remote is available
# to both flatpak-preinstall and brew-setup without baking mutable /var state.
install -d -m0755 /etc/flatpak/remotes.d
curl --fail --retry 3 --silent --show-error \
    --output /etc/flatpak/remotes.d/flathub.flatpakrepo \
    https://dl.flathub.org/repo/flathub.flatpakrepo

disable_unit flatpak-add-fedora-repos.service

# Keep image updates under uupd/bootc rather than the legacy rpm-ostree path.
disable_unit rpm-ostree.service
systemctl mask bootc-fetch-apply-updates.timer bootc-fetch-apply-updates.service

# SSH follows TunaOS's convention: closed in published images, opt-in for a
# local debug build. The preset must agree or first-boot preset-all will undo
# the build-time enablement.
if [[ "${ENABLE_SSHD:-0}" == "1" ]]; then
    enable_optional_unit sshd.service
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
else
    echo "Required global user setup unit is missing: ublue-user-setup.service" >&2
    exit 1
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
enable_required_unit uupd.timer
# Avoid pulling the distrobox module on every update, as in Bluefin LTS.
sed -i 's|uupd|& --disable-module-distrobox|' /usr/lib/systemd/system/uupd.service

# Hummingbird's resolved unit defaults to a disconnected private /tmp. The
# Bluefin LTS workaround is needed for bootc's early-boot DNS path.
sed -i 's@^PrivateTmp=.*@PrivateTmp=no@' /usr/lib/systemd/system/systemd-resolved.service
rm -rf /tmp/uupd

# Build-only extension tooling is not part of the desktop image.
DNF="$(command -v dnf5 || command -v dnf)"
"$DNF" -y remove --no-autoremove dbus-devel glib2-devel meson sassc unzip

echo "Utah desktop service configuration complete"
