#!/usr/bin/env bash
# Configure the Utah live ISO after Flatpaks are baked in. This is the
# non-composefs/bootcDirect branch of Dakota's installer integration.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER_APP_ID=org.bootcinstaller.Installer
TARGET_IMAGE="${TARGET_IMAGE:-ghcr.io/projectbluefin/utah:testing}"

# Live media must not apply update or unified-storage policy intended for an
# installed image. The payload embedded in the squashfs is the install source.
systemctl mask bootc-unified-storage.service || true

useradd --create-home --uid 1000 --user-group --comment 'Live User' liveuser || true
passwd --delete liveuser
printf 'liveuser ALL=(ALL) NOPASSWD: ALL\n' >/etc/sudoers.d/liveuser
chmod 0440 /etc/sudoers.d/liveuser
mkdir -p /home/liveuser/.config
: >/home/liveuser/.config/gnome-initial-setup-done
chown -R liveuser:liveuser /home/liveuser/.config

mkdir -p /etc/gdm
cat >/etc/gdm/custom.conf <<'EOF'
[daemon]
AutomaticLoginEnable=True
AutomaticLogin=liveuser
EOF

# Keep the live session awake, suppress the first-run tour, and pin the
# installer where a person expects it. The installed system retains its normal
# Bluefin desktop dconf policy.
mkdir -p /etc/dconf/db/distro.d /etc/dconf/db/distro.d/locks
cat >/etc/dconf/db/distro.d/50-utah-live <<'EOF'
[org/gnome/shell]
welcome-dialog-last-shown-version='999'
favorite-apps=['utah-installer.desktop', 'org.mozilla.firefox.desktop', 'org.gnome.Nautilus.desktop', 'io.github.kolunmi.Bazaar.desktop']

[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
power-button-action='nothing'
EOF
cat >/etc/dconf/db/distro.d/locks/50-utah-live <<'EOF'
/org/gnome/shell/favorite-apps
/org/gnome/desktop/screensaver/lock-enabled
/org/gnome/desktop/screensaver/idle-activation-enabled
/org/gnome/desktop/session/idle-delay
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-type
/org/gnome/settings-daemon/plugins/power/power-button-action
EOF
dconf update || true
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target || true

# Large image blobs must not exhaust the small live overlay. Fisherman uses
# /var/tmp for staging, so size it from available RAM as Dakota does.
cat >/usr/lib/systemd/system/var-tmp.mount <<'EOF'
[Unit]
Description=Large tmpfs for Utah live installer staging

[Mount]
What=tmpfs
Where=/var/tmp
Type=tmpfs
Options=size=80%,nr_inodes=1m

[Install]
WantedBy=local-fs.target
EOF
systemctl enable var-tmp.mount || true

# Utah is a conventional bootc image rather than Dakota's composefs runtime.
# The ISO builder embeds its OCI payload as a VFS containers-storage graphroot.
mkdir -p /var/lib/containers/storage /etc/containers
cat >/etc/containers/storage.conf <<'EOF'
[storage]
driver = "vfs"
runroot = "/run/containers/storage"
graphroot = "/var/lib/containers/storage"
EOF
mkdir -p /var/fisherman-tmp

# Installer branding/configuration. The custom recipe must reference the same
# tag that the ISO builder imports into the VFS store, never the build host's
# localhost source ref.
mkdir -p /usr/share/bootc-installer/images /etc/bootc-installer
install -Dm0644 /usr/share/ublue-os/bluefin-logos/chicken.png \
    /usr/share/bootc-installer/images/utahraptor.png
cp "${SCRIPT_DIR}/etc/bootc-installer/images.json" /etc/bootc-installer/images.json
python3 - <<PY
import json
from pathlib import Path
ref = "${TARGET_IMAGE}"
for path in (Path("/etc/bootc-installer/images.json"), Path("${SCRIPT_DIR}/etc/bootc-installer/recipe.json")):
    data = json.loads(path.read_text())
    if path.name == "images.json":
        data["default_image"] = ref
        data["images"][0]["imgref"] = ref
    else:
        data["imgref"] = ref
        data["targetImgref"] = ref
        data["image"] = ""
        data["local_imgref"] = f"containers-storage:{ref}"
        data["bootloader"] = "grub2"
        data["composeFsBackend"] = False
    Path("/etc/bootc-installer" , path.name).write_text(json.dumps(data, indent=2) + "\n")
PY
touch /etc/bootc-installer/live-iso-mode

# The Flatpak sees the host configuration under /run/host. Provide both an
# autostart entry and an application entry for manual relaunch from the dock.
mkdir -p /etc/xdg/autostart /usr/share/applications
cat >/etc/xdg/autostart/utah-installer.desktop <<EOF
[Desktop Entry]
Name=Utah Installer
Exec=flatpak run --env=BOOTC_CUSTOM_RECIPE=/run/host/etc/bootc-installer/recipe.json ${INSTALLER_APP_ID}
Icon=bluefin
Type=Application
X-GNOME-Autostart-enabled=true
EOF
cat >/usr/share/applications/utah-installer.desktop <<EOF
[Desktop Entry]
Name=Utah Installer
Comment=Install Utahraptor to your computer
Exec=flatpak run --env=BOOTC_CUSTOM_RECIPE=/run/host/etc/bootc-installer/recipe.json ${INSTALLER_APP_ID}
Icon=bluefin
Type=Application
Categories=System;
EOF

# The installer invokes fisherman through pkexec. Its bundle is not visible to
# host polkit, so expose the binary and authorize only the local live user.
installer_dir="$(find "/var/lib/flatpak/app/${INSTALLER_APP_ID}" -name fisherman -type f 2>/dev/null | head -1 | xargs -r dirname)"
if [[ -z "${installer_dir}" ]]; then
    echo "bootc-installer bundle did not provide fisherman" >&2
    exit 1
fi
mkdir -p /var/usrlocal/bin
ln -sfn "${installer_dir}/fisherman" /var/usrlocal/bin/fisherman
mkdir -p /usr/share/polkit-1/actions /etc/polkit-1/rules.d
cat >/usr/share/polkit-1/actions/org.bootcinstaller.Installer.policy <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN" "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <action id="org.tunaos.Installer.install">
    <description>Install Utah to disk</description>
    <message>Authentication is required to install Utah</message>
    <defaults><allow_any>no</allow_any><allow_inactive>no</allow_inactive><allow_active>yes</allow_active></defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/local/bin/fisherman</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>
</policyconfig>
EOF
cat >/etc/polkit-1/rules.d/99-utah-live-installer.rules <<'EOF'
polkit.addRule(function(action, subject) {
    if ((action.id === "org.freedesktop.policykit.exec" ||
         action.id === "org.tunaos.Installer.install") &&
        subject.user === "liveuser" && subject.local) {
        return polkit.Result.YES;
    }
});
EOF

cat >/usr/lib/systemd/system/utah-live-ready.service <<'EOF'
[Unit]
Description=Utah live environment ready marker
After=display-manager.service
Requires=display-manager.service

[Service]
Type=oneshot
ExecStart=/bin/echo UTAH_LIVE_READY
StandardOutput=tty
TTYPath=/dev/ttyS0

[Install]
WantedBy=multi-user.target
EOF
mkdir -p /etc/systemd/system-preset
cat >/etc/systemd/system-preset/90-utah-live.preset <<'EOF'
enable utah-live-ready.service
enable var-tmp.mount
EOF
systemctl enable utah-live-ready.service

if [[ "${DEBUG:-0}" == 1 ]]; then
    echo 'liveuser:live' | chpasswd
    passwd --unlock root
    echo 'root:root' | chpasswd
    echo 'enable sshd.service' >>/etc/systemd/system-preset/90-utah-live.preset
    cat >>/etc/ssh/sshd_config <<'EOF'
PasswordAuthentication yes
PermitRootLogin yes
EOF
    systemctl enable sshd.service
fi
