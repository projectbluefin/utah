#!/usr/bin/bash
# Configure Utah's desktop services in the image, following bluefin-lts's
# build_scripts/40-services.sh. Hummingbird deliberately ships a server preset;
# these enablements are the part that turns the installed GNOME packages into a
# booting workstation.

set -eoux pipefail

# Lid and sleep actions stay at systemd-logind's defaults (suspend), as on
# Dakota, which ships no logind or sleep override. Utah has only zram swap,
# so suspend-then-hibernate has no resume target and cannot work.

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
# GNOME's system services, from gnome-build-meta's 80-gnome.preset as Dakota
# ships it (files/systemd-presets/system-preset/80-gnome.preset).
enable_unit colord.service
enable_unit accounts-daemon.service
# Bluefin defaults for mDNS discovery and hybrid-GPU application launching.
enable_unit avahi-daemon.service
enable_unit avahi-daemon.socket
enable_unit switcheroo-control.service
enable_unit firewalld.service
# Workstation firewall as Dakota ships it: GNOME OS's lax zone (open
# 1025-65535, mDNS, Samba client, ssh) as the default, with a loose IPv6
# reverse-path filter. gnome-os-config-firewalld ships the zone; these are
# gnome-build-meta gnomeos-deps/firewalld.bst's two firewalld.conf edits.
# --follow-symlinks keeps firewalld's variant symlink in place.
test -f /usr/lib/firewalld/zones/lax.xml
sed -i --follow-symlinks \
    -e 's/^DefaultZone=.*/DefaultZone=lax/' \
    -e 's/^IPv6_rpfilter=.*/IPv6_rpfilter=loose/' /etc/firewalld/firewalld.conf
grep -qx 'DefaultZone=lax' /etc/firewalld/firewalld.conf
grep -qx 'IPv6_rpfilter=loose' /etc/firewalld/firewalld.conf
enable_unit fwupd.service
enable_unit fwupd-refresh.timer
enable_unit dconf-update.service
enable_unit tailscaled.service
enable_unit uupd.timer
enable_unit projectbluefin-countme.timer
# Dakota's two monthly-wallpaper units (#1266): ublue-wallpaper-month from
# bluefin/wallpaper-month.bst and bluefin-wallpaper-month from common.bst. See
# the zz0 check below for why nothing reads the links they keep.
enable_unit ublue-wallpaper-month.service
enable_unit bluefin-wallpaper-month.service
enable_unit ublue-system-setup.service
enable_unit systemd-resolved.service
# See the preset: gated on systemd-boot by drop-in, skipped elsewhere (#363).
enable_unit systemd-boot-update.service
enable_unit bootc-unified-storage.timer
# Dakota desktop parity (#675): dakota-config-firstboot-date ships these units
# with Dakota's static multi-user.target.wants links (#1265); the preset
# agrees so bootc's first-boot preset application keeps them enabled (see
# #98). ublue-firstboot-date.service has no [Install]: its path unit starts it
# once systemd-timesyncd reports a sync, which needs timesyncd in place of
# chrony (#1228).
enable_unit ublue-firstboot-date.path
enable_unit ublue-booted-image.service
# Removes the live ISO's bootc installer Flatpak from an installed system, once.
enable_unit bluefin-remove-installer.service
# Apply Dakota's runtime desktop integrations onto common's files. Each patch
# is guarded: already-patched is a no-op, an unrecognized upstream file fails
# the build loudly instead of silently shipping unpatched behavior.
# Monthly wallpaper, as Dakota's effective image has it (#1266): both month
# units keep their links current, but the default stays common's zz0 value.
# Dakota's common.bst repoints zz0 from 12-bluefin.xml to
# /var/lib/ublue-os/current-bluefin.xml, and common names another month, so
# that sed matches nothing (projectbluefin/dakota#1902) and common's per-user,
# hemisphere-aware bluefin-dynamic-wallpaper rotator stays in charge. Fail if
# common ever names 12-bluefin.xml: Dakota's repoint would then take effect
# there, and Utah has to follow it. The contract fails the build if common
# drops the file.
ZZ0_OVERRIDE=/usr/share/glib-2.0/schemas/zz0-bluefin-modifications.gschema.override
if [ -f "${ZZ0_OVERRIDE}" ]; then
    if grep -qF 'file:///usr/share/backgrounds/bluefin/12-bluefin.xml' "${ZZ0_OVERRIDE}"; then
        echo "ERROR: ${ZZ0_OVERRIDE} names 12-bluefin.xml, so Dakota's current-bluefin.xml repoint now applies -- re-fit it (projectbluefin/dakota#1902)" >&2
        exit 1
    elif ! grep -qE "^picture-uri='file:///usr/share/backgrounds/bluefin/[0-9]{2}-bluefin\.xml'$" "${ZZ0_OVERRIDE}"; then
        echo "ERROR: ${ZZ0_OVERRIDE} changed upstream -- re-check Dakota's wallpaper repoint (projectbluefin/dakota#1902)" >&2
        exit 1
    fi
fi
# Installation date for fastfetch: read the first-boot timestamp instead of
# inferring the image age from /var/log timestamps.
FASTFETCH_JSONC=/usr/share/ublue-os/fastfetch.jsonc
if [ -f "${FASTFETCH_JSONC}" ]; then
    if grep -qF '/var/lib/ublue-os/install-date' "${FASTFETCH_JSONC}"; then
        : already patched
    elif grep -qF 'ls -alct /var/log' "${FASTFETCH_JSONC}"; then
        sed -i "s#date -d\$(ls -alct /var/log --time-style=full-iso|tail -1|awk '{print \$6}')#date -d\$(cat /var/lib/ublue-os/install-date 2>/dev/null || date +%Y-%m-%d)#" "${FASTFETCH_JSONC}"
    else
        echo "ERROR: ${FASTFETCH_JSONC} changed upstream -- re-fit the install-date patch (see ublue-firstboot-date.service)" >&2
        exit 1
    fi
fi
# Weekly community stats: Dakota drops common's two fastfetch lines and the
# break after them (patches/common/0006-fastfetch-drop-weekly-stats.patch)
# and fetches nothing at runtime, so Utah does the same (#1266). Already
# dropped is a no-op; any other shape fails the build.
if [ -f "${FASTFETCH_JSONC}" ]; then
    python3 - "${FASTFETCH_JSONC}" <<'PY'
import re
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    text = f.read()
stats = ("/usr/share/ublue-os/fastfetch-user-count", "/usr/share/ublue-os/bazaar-install-count")
if not any(stat in text for stat in stats):
    sys.exit(0)
# One object per stat: its lines up to the closing brace, which no inner line
# matches (the format strings carry {1}, so a brace-free match cannot work).
body = r'(?:(?![ \t]*\},?\n)[^\n]*\n)*?'
entry = r'\{\n' + body + r'[ \t]*"text": "cat %s",\n' + body + r'[ \t]*\},\s*'
pattern = "".join(entry % re.escape(stat) for stat in stats) + r'"break",\s*'
text, count = re.subn(pattern, "", text)
if count != 1:
    sys.exit(f"ERROR: {path} changed upstream -- re-fit Dakota's weekly-stats drop (patches/common/0006)")
with open(path, "w", encoding="utf-8") as f:
    f.write(text)
PY
fi
# Ghostty's skeleton config follows GNOME's light/dark style, as Dakota
# patches it (patches/common/0002-ghostty-dual-theme.patch, #1266).
GHOSTTY_SKEL=/etc/skel/.config/ghostty/config.ghostty
GHOSTTY_DUAL_THEME='theme = light:Catppuccin Latte,dark:Catppuccin Mocha'
if [ -f "${GHOSTTY_SKEL}" ]; then
    if grep -qxF "${GHOSTTY_DUAL_THEME}" "${GHOSTTY_SKEL}"; then
        : already patched
    elif grep -qxF 'theme = Catppuccin Mocha' "${GHOSTTY_SKEL}"; then
        sed -i "s/^theme = Catppuccin Mocha\$/${GHOSTTY_DUAL_THEME}/" "${GHOSTTY_SKEL}"
    else
        echo "ERROR: ${GHOSTTY_SKEL} changed upstream -- re-fit Dakota's dual-theme patch (patches/common/0002)" >&2
        exit 1
    fi
fi
# Booted image tag for fastfetch: Common's image-info script asks
# /usr/libexec/ublue-booted-image, which falls back to the boot-time snapshot
# ublue-booted-image.service writes when unprivileged `bootc status` fails
# (bootc >= 1.14). Nothing to patch; fail if Common drops that fallback.
IMAGE_INFO_SH=/usr/bin/ublue-image-info.sh
BOOTED_IMAGE_BIN=/usr/libexec/ublue-booted-image
if [ -f "${IMAGE_INFO_SH}" ]; then
    # Common's legacy lock glyph probes rpm-ostree, which Hummingbird does
    # not ship. It is not a bootc signature check; omit the whole glyph block
    # rather than hide its stderr or imply the image is unlocked (#931).
    # Common ships it bare or wrapped in a `command -v rpm-ostree` guard;
    # strip either form. Both deletions are safe to repeat.
    sed -i -e '/^if command -v rpm-ostree >\/dev\/null 2>&1; then$/,/^fi$/d' \
        -e '/^if \[\[ "$(rpm-ostree status --booted)" =~ "signed" \]\]; then$/,/^fi$/d' "${IMAGE_INFO_SH}"
    if grep -v '^[[:space:]]*#' "${IMAGE_INFO_SH}" | grep -qF 'rpm-ostree'; then
        echo "ERROR: ${IMAGE_INFO_SH} changed upstream -- re-fit the rpm-ostree removal" >&2
        exit 1
    fi
    if ! grep -qF "${BOOTED_IMAGE_BIN}" "${IMAGE_INFO_SH}" \
        || [ ! -x "${BOOTED_IMAGE_BIN}" ] \
        || ! grep -qF '/run/ublue-os/booted-image' "${BOOTED_IMAGE_BIN}"; then
        echo "ERROR: ${IMAGE_INFO_SH} or ${BOOTED_IMAGE_BIN} changed upstream -- re-check the booted-image snapshot fallback (see ublue-booted-image.service)" >&2
        exit 1
    fi
fi
# Non-interactive login shells (notably GDM's) source /etc/profile too: keep
# uwelcome from activating the desktop portal outside interactive shells.
UWELCOME_SH=/etc/profile.d/uwelcome.sh
if [ -f "${UWELCOME_SH}" ]; then
    if grep -qF 'case $- in' "${UWELCOME_SH}"; then
        : already patched
    elif grep -qF 'UWELCOME_SHOWN' "${UWELCOME_SH}"; then
        sed -i '1a\
\
# GDM sources /etc/profile before graphical-session.target is active.\
# Avoid portal activation and exporting UWELCOME_SHOWN into the desktop.\
# Only interactive shells should run the greeting.\
case $- in\
    *i*) ;;\
    *) return 0 ;;\
esac' "${UWELCOME_SH}"
    else
        echo "ERROR: ${UWELCOME_SH} changed upstream -- re-fit the interactive-shell guard" >&2
        exit 1
    fi
fi
# Common's System Update, Community and Documentation launchers duplicate
# Control Center (updates, the Community link, docs); Dakota drops all three
# (projectbluefin/dakota elements/bluefin/common.bst). `ujust update` stays.
rm -f /usr/share/applications/system-update.desktop \
    /usr/share/applications/discourse.desktop \
    /usr/share/applications/documentation.desktop
enable_unit ModemManager.service
# Power profiles and thermal management as Dakota ships them (#388):
# power-profiles-daemon, not TuneD, and no intel_lpmd.
enable_unit power-profiles-daemon.service
enable_unit thermald.service
# Dakota's effective service state (#1233); see 85-utah-desktop.preset.
enable_unit rtkit-daemon.service
enable_unit uresourced.service
enable_unit upower.service
enable_unit udisks2.service
enable_unit polkit-agent-helper.socket
enable_unit cups.service
enable_unit pcscd.socket
enable_unit gnome-remote-desktop.service
enable_unit gnome-remote-desktop-configuration.service
enable_unit malcontent-timerd.service
enable_unit malcontent-timer-extension-agent.service
enable_unit malcontent-webd.service
enable_unit malcontent-webd-update.timer
enable_unit systemd-oomd.service
enable_unit systemd-oomd.socket
enable_unit wpa_supplicant.service
enable_unit podman.socket
enable_unit podman-auto-update.timer
enable_unit podman-restart.service
enable_unit podman-clean-transient.service
enable_unit systemd-sysext.socket
enable_unit systemd-factory-reset.socket
enable_unit systemd-pcrextend.socket
enable_unit systemd-storage-block.socket
enable_unit systemd-storage-fs.socket
enable_unit systemd-udev-load-credentials.service
enable_unit systemd-userdb-load-credentials.service
disable_unit multipathd.service
disable_unit lm_sensors.service
# nfs-client.target is turned off: see 85-utah-desktop.preset.
disable_unit nfs-client.target
# sysstat collection is opt-in, as in Dakota (elements/bluefin/sysstat.bst);
# the package stays for on-demand sar/iostat. See 85-utah-desktop.preset.
disable_unit sysstat.service
disable_unit sysstat-collect.timer
disable_unit sysstat-summary.timer
disable_unit sysstat-rotate.timer
# kanata replaces input-remapper and is opt-in, as on Dakota (#1222); see
# 85-utah-desktop.preset.
disable_unit kanata.service
# Printing on demand, as Fedora's preset enables it for Bluefin; Hummingbird's
# 99-default-disable would leave it off.
enable_unit cups.socket
enable_unit cups.path

# Bluefin's Brewfile and Bazaar preinstall hook need the Flathub remote before
# first boot. Keep this as a .flatpakrepo descriptor so the remote is available
# to both flatpak-preinstall and brew-setup without baking mutable /var state.
#
# The descriptor is pinned like every other download in this build: it carries
# Url= and GPGKey=, so it is the trust root every Flatpak on the image is
# verified against, not merely a pointer to one. OSTree's signature check is
# only as good as the key this file names. Flathub's key has not changed since
# the repository opened, so a hash bump here means Flathub itself moved and the
# new descriptor deserves a look before it ships.
FLATHUB_REPO_SHA256=3371dd250e61d9e1633630073fefda153cd4426f72f4afa0c3373ae2e8fea03a
install -d -m0755 /etc/flatpak/remotes.d
curl --fail --retry 3 --silent --show-error \
    --output /tmp/flathub.flatpakrepo \
    https://dl.flathub.org/repo/flathub.flatpakrepo
echo "${FLATHUB_REPO_SHA256}  /tmp/flathub.flatpakrepo" | sha256sum --check --strict
install -m0644 /tmp/flathub.flatpakrepo /etc/flatpak/remotes.d/flathub.flatpakrepo
rm -f /tmp/flathub.flatpakrepo
# The pinned descriptor carries no collection ID, but common's preinstall.d
# entries pin CollectionID=org.flathub.Stable, and flatpak preinstall skips a
# remote whose collection ID differs -- silently: "Nothing to do." and no
# Bazaar. A remotes.d remote is applied to the repo once, at creation, so the
# ID has to be in the descriptor before first boot. It is added after the
# hash check above, which therefore still covers Flathub's bytes exactly.
# Bluefin's remote-add'ed remote picks the ID up from Flathub's summary.
grep -q '^DeployCollectionID=' /etc/flatpak/remotes.d/flathub.flatpakrepo ||
    sed -i '/^\[Flatpak Repo\]$/a DeployCollectionID=org.flathub.Stable' \
        /etc/flatpak/remotes.d/flathub.flatpakrepo
grep -qx 'DeployCollectionID=org.flathub.Stable' /etc/flatpak/remotes.d/flathub.flatpakrepo
# BudsLink backs the BudsLink-Companion extension enabled by default; Dakota
# appends it to common's system Flatpaks and preinstalls it
# (projectbluefin/dakota elements/bluefin/common.bst; the preinstall comes from
# the factory's dakota-config-flatpak-apps). The desktop contract's [flatpak]
# apps list fails the build if it is missing.
SYSTEM_FLATPAKS=/usr/share/ublue-os/homebrew/system-flatpaks.Brewfile
if [ -f "${SYSTEM_FLATPAKS}" ] && ! grep -qxF 'flatpak "io.github.maniacx.BudsLink"' "${SYSTEM_FLATPAKS}"; then
    echo 'flatpak "io.github.maniacx.BudsLink"' >>"${SYSTEM_FLATPAKS}"
fi
# Papers, baobab and Sushi ship as native packages, as on Dakota (#1270), so
# their Flatpaks leave common's system set. The contract's [flatpak] apps list
# is compared with the Brewfile line for line and fails if this misses.
# Dakota also retargets common's logo-menu Documentation entry from the Papers
# Flatpak to /usr/bin/papers; Utah's own 04-bluefin-custom-command-menu
# replaces that entry with the docs.projectbluefin.io link, so there is
# nothing to retarget here.
if [ -f "${SYSTEM_FLATPAKS}" ]; then
    sed -i -E '/^flatpak "org\.gnome\.(Papers|NautilusPreviewer|baobab)"$/d' "${SYSTEM_FLATPAKS}"
fi

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
#
# The desktop's per-user services. Hummingbird's user preset enables only
# dbus and then disables everything else. Fedora's (and so Bluefin's) enables
# these, and without them an installed Utah had no audio server at all. See
# /usr/lib/systemd/user-preset/85-utah-desktop.preset; the desktop contract
# asserts the result (services.user_enabled).
# grub-boot-success.timer is deliberately absent: /boot is read-only at runtime
# so the mark can never be written (see the user preset, #364).
# speech-dispatcher.socket and xdg-desktop-portal-rewrite-launchers.service
# follow Dakota, which inherits them from gnome-build-meta's user presets.
for unit in pipewire.socket pipewire-pulse.socket wireplumber.service \
            xdg-user-dirs.service \
            obex.service mpris-proxy.service \
            speech-dispatcher.socket xdg-desktop-portal-rewrite-launchers.service; do
    if user_unit_exists "${unit}"; then
        systemctl --global enable "${unit}"
    else
        echo "user unit ${unit} is not installed; skipping" >&2
    fi
done
if user_unit_exists podman-auto-update.timer; then
    systemctl --global enable podman-auto-update.timer
fi
if user_unit_exists ublue-user-setup.service; then
    systemctl --global enable ublue-user-setup.service
fi
# The Secret Service starts with the session, as GNOME OS links oo7-daemon
# into gnome-session.target.wants (#1227). Required: without it nothing
# unlocks the login keyring.
user_unit_exists oo7-daemon.service
systemctl --global enable oo7-daemon.service

# Common's user preset is not applied by bootc's system preset-all. Without
# a global enablement, ChairLift's GUI Brewfile is present but never installed.
# This is required desktop setup: an absent unit or failed enablement must fail
# the image build, rather than leave a Control Center launcher with no GUI.
user_unit_exists brew-preinstall.service
systemctl --global enable brew-preinstall.service

# Match Bluefin's login behavior. The operations are idempotent and authselect
# is present in the Hummingbird base.
authselect enable-feature with-silent-lastlog
authselect enable-feature with-fingerprint
# Changing the password re-encrypts the oo7 login keyring too, as Dakota's
# bluefin/oo7-passwd-pam.bst arranges (#1227). shadow-utils' passwd stack
# already calls the keyring module after its system-auth substack; point it at
# pam_oo7 instead of the removed pam_gnome_keyring. gdm 51's own stacks call
# pam_oo7 already. Already switched is a no-op; any other shape fails.
PASSWD_PAM=/etc/pam.d/passwd
if [ -f "${PASSWD_PAM}" ]; then
    if grep -qE '^-password[[:space:]]+optional[[:space:]]+pam_oo7\.so use_authtok$' "${PASSWD_PAM}"; then
        : already switched
    elif grep -qE '^-password[[:space:]]+optional[[:space:]]+pam_gnome_keyring\.so use_authtok$' "${PASSWD_PAM}" \
        && grep -qE '^password[[:space:]]+substack[[:space:]]+system-auth$' "${PASSWD_PAM}"; then
        sed -i -E 's/pam_gnome_keyring\.so use_authtok$/pam_oo7.so use_authtok/' "${PASSWD_PAM}"
    else
        echo "ERROR: ${PASSWD_PAM} changed upstream -- re-fit the pam_oo7 password hook (Dakota bluefin/oo7-passwd-pam.bst)" >&2
        exit 1
    fi
fi

# sudo-rs is the sudo on PATH, as on Dakota (bluefin/sudo-rs.bst, #1229). The
# factory's sudo-rs installs only sudo-rs/su-rs/visudo-rs and requires C sudo,
# whose /etc/pam.d/sudo, sudo-i and /etc/sudoers it reads -- Dakota keeps those
# files the same way. Dakota's compose overwrites /usr/bin/sudo and sudoedit;
# here they become links to the setuid sudo-rs, which dispatches on the name
# it was run as. su and visudo stay util-linux's and C sudo's, as on Dakota.
test -u /usr/bin/sudo-rs
ln -sfn sudo-rs /usr/bin/sudo
ln -sfn sudo-rs /usr/bin/sudoedit
[ "$(readlink /usr/bin/sudo)" = sudo-rs ]
# uutils-coreutils sits beside GNU coreutils rather than over it (#1229).
# Dakota overwrites the GNU names, but here GNU's files belong to the
# coreutils-single RPM that every later package scriptlet, dracut's initramfs
# rebuild and SELinux relabelling in this build run against. Utah exposes uutils
# under Dakota's own uutils-<prog> names, next to the RPM's uu_<prog> links.
test -x /usr/bin/uutils-coreutils
for prog in $(/usr/bin/uutils-coreutils --list); do
    ln -sfn uutils-coreutils "/usr/bin/uutils-${prog}"
done
[ "$(readlink /usr/bin/uutils-ls)" = uutils-coreutils ]

# uupd is distributed as a release binary, not an RPM in the Hummingbird
# repositories. Keep its version pinned by the Containerfile argument. Its
# units, uupd-manual.service, polkit rule and /etc/uupd/config.json come from
# dakota-config-uupd (Dakota's elements/bluefin/uupd.bst), which replaces
# Common's four-a-day uupd.timer with the daily 04:00 one, as on Dakota.
install -Dm0755 /tmp/uupd/uupd /usr/bin/uupd

# Hummingbird's resolved unit defaults to a disconnected private /tmp. The
# Bluefin LTS workaround is needed for bootc's early-boot DNS path.
sed -i 's@^PrivateTmp=.*@PrivateTmp=no@' /usr/lib/systemd/system/systemd-resolved.service
rm -rf /tmp/uupd

# Build-only extension tooling (GSConnect's meson build) is not part of the
# desktop image.
#
# No --no-autoremove: that flag kept the build dependency closure (ninja-build,
# meson-srpm-macros, *-devel chains) in the shipping image. The default remove
# cleans up dependencies orphaned by this transaction, which is exactly the
# closure. Decisions D1 (docs/bluefin-package-gaps.md).
DNF="$(command -v dnf5 || command -v dnf)"
"$DNF" -y remove dbus-devel glib2-devel meson
for pkg in dbus-devel glib2-devel meson; do
    if rpm -q "$pkg" >/dev/null 2>&1; then
        echo "ERROR: build tooling $pkg still installed after removal" >&2
        exit 1
    fi
done

echo "Utah desktop service configuration complete"
