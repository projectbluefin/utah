#!/usr/bin/env bash
# Configure the Utah live session. This deliberately starts small: boot a
# faithful Utah desktop first, then add installer-specific payloads once the
# ISO boot path is stable.
set -euo pipefail

# The installed image's experimental unified-storage service cannot pull a
# localhost image from inside the live VM. It is not needed while live.
systemctl mask bootc-unified-storage.service || true

useradd --create-home --uid 1000 --user-group --comment 'Live User' liveuser || true
passwd --delete liveuser
printf 'liveuser ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/liveuser
chmod 0440 /etc/sudoers.d/liveuser

mkdir -p /home/liveuser/.config
: > /home/liveuser/.config/gnome-initial-setup-done
chown -R liveuser:liveuser /home/liveuser/.config

mkdir -p /etc/gdm
cat > /etc/gdm/custom.conf <<'EOF'
[daemon]
AutomaticLoginEnable=True
AutomaticLogin=liveuser
EOF

# Keep the live desktop awake and prevent a laptop lid from suspending the
# disposable session while an installer or diagnostic command is running.
mkdir -p /etc/dconf/db/distro.d /etc/dconf/db/distro.d/locks
cat > /etc/dconf/db/distro.d/50-utah-live <<'EOF'
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
cat > /etc/dconf/db/distro.d/locks/50-utah-live <<'EOF'
/org/gnome/desktop/screensaver/lock-enabled
/org/gnome/desktop/screensaver/idle-activation-enabled
/org/gnome/desktop/session/idle-delay
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-type
/org/gnome/settings-daemon/plugins/power/power-button-action
EOF
dconf update || true

# An explicit marker gives QEMU/CI a stable readiness signal without relying on
# journal formatting. TTYPath keeps it on the serial console.
cat > /usr/lib/systemd/system/utah-live-ready.service <<'EOF'
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
# Hummingbird runs preset-all on first boot and removes enablements not listed
# by a preset, even when they were made during the image build.
echo 'enable utah-live-ready.service' > /etc/systemd/system-preset/90-utah-live.preset
systemctl enable utah-live-ready.service

if [[ "${DEBUG:-0}" == 1 ]]; then
    # Debug-only SSH is intentionally password based and confined to this
    # disposable live image. Published Utah images remain key-only/disabled.
    echo 'liveuser:live' | chpasswd
    passwd --unlock root
    echo 'root:root' | chpasswd
    echo 'enable sshd.service' >> /etc/systemd/system-preset/90-utah-live.preset
    cat >> /etc/ssh/sshd_config <<'EOF'
PasswordAuthentication yes
PermitRootLogin yes
EOF
    systemctl enable sshd.service
fi
