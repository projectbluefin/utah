#!/usr/bin/bash
# Configure Utah's desktop services in the image, following bluefin-lts's
# build_scripts/40-services.sh. Hummingbird deliberately ships a server preset;
# Utah's presets and the configuration here are the part that turns the
# installed GNOME packages into a booting workstation.

set -eoux pipefail

# Lid and sleep actions stay at systemd-logind's defaults (suspend), as on
# Dakota, which ships no logind or sleep override. Utah's swap is zswap over
# /var/swap/swapfile with no resume= argument, as on Dakota, so
# suspend-then-hibernate has no resume target and cannot work.

user_unit_exists() {
    for dir in /usr/lib/systemd/user /usr/local/lib/systemd/user /etc/systemd/user; do
        [ -e "$dir/$1" ] && return 0
    done
    return 1
}

# System services follow the presets, enabled by default as on Dakota (#1233):
# 98-utah-enable-default.preset turns on every installed unit no preset turns
# off, and 85-utah-desktop.preset overrides the Hummingbird base where Utah
# differs. `systemctl preset-all` near the end of this script applies them.
#
# rechunker-group-fix.service is removed outright. It exists to repair the
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
rm -f /usr/lib/systemd/system/rechunker-group-fix.service
rm -rf /usr/lib/systemd/system/rechunker-group-fix.service.d
rm -f /usr/lib/systemd/system/*.wants/rechunker-group-fix.service \
      /etc/systemd/system/*.wants/rechunker-group-fix.service
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

# Keep image updates under uupd/bootc rather than the legacy rpm-ostree path.
# Mask in /usr/lib as well as /etc so the mask survives cross-vendor /etc 3-way merges
# when switching from systems that have bootc-fetch-apply-updates enabled (such as Bluefin).
# In a cross-vendor switch, ostree's 3-way /etc merge carries timers.target.wants/
# symlinks into /etc, which would re-enable the timer if /usr/lib was unmasked.
# rpm-ostree.service is off by 85-utah-desktop.preset.
systemctl mask bootc-fetch-apply-updates.timer bootc-fetch-apply-updates.service

# chrony is removed from the base ([excluded] in base.toml, #1228: time sync is
# systemd-timesyncd, as on Dakota). Erasing the package drops its sysusers.d
# file but leaves the account behind, and `bootc container lint --fatal-warn`
# fails on an /etc/passwd entry with no sysusers.d declaration. Drop the
# orphaned account with the package.
if ! rpm -q --quiet chrony; then
    for f in /etc/passwd /etc/group /etc/shadow /etc/gshadow; do
        if [ -f "${f}" ]; then
            sed -i '/^chrony:/d' "${f}"
        fi
    done
fi
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
# local debug build. preset-all below and first-boot preset application both
# read the preset, so the debug build flips the preset line.
if [[ "${ENABLE_SSHD:-0}" == "1" ]]; then
    sed -i 's/^disable sshd.service$/enable sshd.service/' \
        /usr/lib/systemd/system-preset/85-utah-desktop.preset
    grep -qx 'enable sshd.service' /usr/lib/systemd/system-preset/85-utah-desktop.preset
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
# Resolve .local names over mDNS through nss-mdns, as Dakota and Bluefin do.
authselect enable-feature with-mdns4

# Dakota's swapfile (/var/swap/swapfile, created at boot by
# bluefin-swapfile-init) needs the swapfile_t label under Utah's SELinux
# policy, which has no rule for that path (#1224). Install the one-line module
# into the policy store, which Hummingbird keeps under /usr/lib/selinux, so it
# ships in the image and survives ostree's per-deployment policy rebuild.
# -n: no reload; an image build has no selinuxfs to load it into.
semodule -n -i /usr/share/selinux/packages/utah-swapfile.cil
if ! matchpathcon /var/swap/swapfile | grep -q ':swapfile_t:'; then
    echo "ERROR: /var/swap/swapfile does not label as swapfile_t; swapon would be denied" >&2
    exit 1
fi

# uupd is distributed as a release binary, not an RPM in the Hummingbird
# repositories. Keep its version pinned by the Containerfile argument. Its
# units, uupd-manual.service, polkit rule and /etc/uupd/config.json come from
# dakota-config-uupd (Dakota's elements/bluefin/uupd.bst), which replaces
# Common's four-a-day uupd.timer with the daily 04:00 one, as on Dakota.
install -Dm0755 /tmp/uupd/uupd /usr/bin/uupd
# Common ships its own /etc/uupd/config.json, so rpm installs the package's
# copy as .rpmnew; Dakota's hardware-check config must win.
if [ -f /etc/uupd/config.json.rpmnew ]; then
    mv -f /etc/uupd/config.json.rpmnew /etc/uupd/config.json
fi

# Apply the system presets to every installed unit, so the image carries the
# state first-boot preset application would give it and the desktop contract
# checks that state. Run after the last unit file lands (uupd above).
systemctl preset-all

# Hummingbird's resolved unit defaults to a disconnected private /tmp. The
# Bluefin LTS workaround is needed for bootc's early-boot DNS path.
sed -i 's@^PrivateTmp=.*@PrivateTmp=no@' /usr/lib/systemd/system/systemd-resolved.service
rm -rf /tmp/uupd

# Build-only extension tooling is not part of the desktop image. unzip stays:
# it is in [parity] as well as [build], because Bluefin ships it to users.
#
# No --no-autoremove: that flag kept the build dependency closure (ninja-build,
# meson-srpm-macros, libsass, *-devel chains) in the shipping image. The
# default remove cleans up dependencies orphaned by this transaction, which is
# exactly the closure. Decisions D1 (docs/bluefin-package-gaps.md).
DNF="$(command -v dnf5 || command -v dnf)"
"$DNF" -y remove dbus-devel glib2-devel meson sassc
for pkg in dbus-devel glib2-devel meson sassc; do
    if rpm -q "$pkg" >/dev/null 2>&1; then
        echo "ERROR: build tooling $pkg still installed after removal" >&2
        exit 1
    fi
done

echo "Utah desktop service configuration complete"
