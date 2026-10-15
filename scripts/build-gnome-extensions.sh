#!/usr/bin/bash

set -eoux pipefail

echo "::group:: ===$(basename "$0")==="

# Build Extensions
# Default enablement is supplied by common's GNOME schema override. It must
# include each extension built below that Bluefin smoke verifies; CI preserves it.

# AppIndicator Support
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/appindicatorsupport@rgcjonas.gmail.com/schemas

# Bazaar Companion
mv /usr/share/gnome-shell/extensions/tmp/bazaar-integration@kolunmi.github.io/src/ /usr/share/gnome-shell/extensions/bazaar-integration@kolunmi.github.io/

# Blur My Shell
make -C /usr/share/gnome-shell/extensions/blur-my-shell@aunetx
unzip -o /usr/share/gnome-shell/extensions/blur-my-shell@aunetx/build/blur-my-shell@aunetx.shell-extension.zip -d /usr/share/gnome-shell/extensions/blur-my-shell@aunetx
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/blur-my-shell@aunetx/schemas
rm -rf /usr/share/gnome-shell/extensions/blur-my-shell@aunetx/build

# Caffeine
# The Caffeine extension is built/packaged into a temporary subdirectory (tmp/caffeine/caffeine@patapon.info).
# Unlike other extensions, it must be moved to the standard extensions directory so GNOME Shell can detect it.
mv /usr/share/gnome-shell/extensions/tmp/caffeine/caffeine@patapon.info /usr/share/gnome-shell/extensions/caffeine@patapon.info
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/caffeine@patapon.info/schemas

# Dash to Dock
make -C /usr/share/gnome-shell/extensions/dash-to-dock@micxgx.gmail.com
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/dash-to-dock@micxgx.gmail.com/schemas

# Gradia Capture
bash /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io/build.sh
unzip -o /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io/gradia-integration@alexandervanhee.github.io.shell-extension.zip -d /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io
rm -f /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io/gradia-integration@alexandervanhee.github.io.shell-extension.zip
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io/schemas

# GSConnect
# Disable update-desktop-database hook since desktop-file-utils is unavailable in Hummingbird/Utah repositories.
gsconnect_dir="/usr/share/gnome-shell/extensions/gsconnect@andyholmes.github.io"
sed -i 's/update_desktop_database: true/update_desktop_database: false/' "${gsconnect_dir}/meson.build"

# GNOME 48+ makes GjsPrivate.DBusImplementation a final GType, and GSConnect
# subclassed it in the Shell clipboard, the daemon's Wayland clipboard and its
# D-Bus utilities. Any one of them throws "Cannot inherit from a final type" at
# module load. Utah used to patch only shell/clipboard.js, so the daemon still
# died on every login and GSConnect never came up. Upstream stopped
# subclassing it in v73 (GSConnect 11be9b7f); fail the build if a submodule
# bump ever brings a subclass back anywhere in the tree.
if grep -rn "extends GjsPrivate.DBusImplementation" "${gsconnect_dir}/src"; then
    echo "gsconnect subclasses the final GjsPrivate.DBusImplementation again; GNOME 51 cannot load it" >&2
    exit 1
fi

meson setup --prefix=/usr "${gsconnect_dir}" "${gsconnect_dir}/_build"
meson install -C "${gsconnect_dir}/_build" --skip-subprojects
# GSConnect installs schemas to /usr/share/glib-2.0/schemas and meson compiles them automatically

# Custom Command Menu
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/custom-command-list@storageb.github.com/schemas

# BudsLink Companion
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/BudsLink-Companion@maniacx.github.com/schemas

# Tailscale Quick Settings
mv /usr/share/gnome-shell/extensions/tmp/tailscale-gnome-qs/tailscale-gnome-qs@tailscale-qs.github.io /usr/share/gnome-shell/extensions/tailscale-gnome-qs@tailscale-qs.github.io

# Bluefin Bling (Syncthing Toggle & Power Status Color)
mv /usr/share/gnome-shell/extensions/tmp/bluefin-bling/extensions/syncthing-toggle /usr/share/gnome-shell/extensions/syncthing-toggle@projectbluefin.io
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/syncthing-toggle@projectbluefin.io/schemas
mv /usr/share/gnome-shell/extensions/tmp/bluefin-bling/extensions/power-status-color /usr/share/gnome-shell/extensions/power-status-color@projectbluefin.io

# Curated release archive extensions (verified against pinned SHA256 digests)
# Tiling Shell
tilingshell_zip="/tmp/tilingshell@ferrarodomenico.com.zip"
tilingshell_sha256="451d6836e2d205d5c96f4abde7cf8e672e30181f722ef8418ad811dcd4c08c1f"
tilingshell_dir="/usr/share/gnome-shell/extensions/tilingshell@ferrarodomenico.com"
curl -fsSL "https://github.com/domferr/tilingshell/releases/download/18.0-candidate/tilingshell@ferrarodomenico.com.zip" -o "${tilingshell_zip}"
echo "${tilingshell_sha256}  ${tilingshell_zip}" | sha256sum --check --strict
mkdir -p "${tilingshell_dir}"
unzip -o "${tilingshell_zip}" -d "${tilingshell_dir}"
rm -f "${tilingshell_zip}"
glib-compile-schemas --strict "${tilingshell_dir}/schemas"

# Quick Settings Audio Panel
qsap_zip="/tmp/quick-settings-audio-panel.zip"
qsap_sha256="3a6da80e9b391251149bcf87a9cb5dbbccee827887d1aef43be4ee090b04a29f"
qsap_dir="/usr/share/gnome-shell/extensions/quick-settings-audio-panel@rayzeq.github.io"
curl -fsSL "https://github.com/Rayzeq/quick-settings-audio-panel/releases/download/v103/quick-settings-audio-panel%40rayzeq.github.io.shell-extension.zip" -o "${qsap_zip}"
echo "${qsap_sha256}  ${qsap_zip}" | sha256sum --check --strict
mkdir -p "${qsap_dir}"
unzip -o "${qsap_zip}" -d "${qsap_dir}"
rm -f "${qsap_zip}"
glib-compile-schemas --strict "${qsap_dir}/schemas"

# Quick Settings Audio Devices Hider
hider_zip="/tmp/quicksettings-audio-devices-hider.zip"
hider_sha256="217082e049572ae54bc90bc09240f896e25f4a2609e0c0aa4b7133bed46d935d"
hider_dir="/usr/share/gnome-shell/extensions/quicksettings-audio-devices-hider@marcinjahn.com"
curl -fsSL "https://extensions.gnome.org/download-extension/quicksettings-audio-devices-hider@marcinjahn.com.shell-extension.zip?version_tag=69924" -o "${hider_zip}"
echo "${hider_sha256}  ${hider_zip}" | sha256sum --check --strict
mkdir -p "${hider_dir}"
unzip -o "${hider_zip}" -d "${hider_dir}"
rm -f "${hider_zip}"
glib-compile-schemas --strict "${hider_dir}/schemas"

# Quick Settings Audio Devices Renamer
renamer_zip="/tmp/quicksettings-audio-devices-renamer.zip"
renamer_sha256="a4f33897983376a23c5de69d6a008d1451afcc05cd7ce3b56630bd739c095a2e"
renamer_dir="/usr/share/gnome-shell/extensions/quicksettings-audio-devices-renamer@marcinjahn.com"
curl -fsSL "https://extensions.gnome.org/download-extension/quicksettings-audio-devices-renamer@marcinjahn.com.shell-extension.zip?version_tag=69925" -o "${renamer_zip}"
echo "${renamer_sha256}  ${renamer_zip}" | sha256sum --check --strict
mkdir -p "${renamer_dir}"
unzip -o "${renamer_zip}" -d "${renamer_dir}"
rm -f "${renamer_zip}"
glib-compile-schemas --strict "${renamer_dir}/schemas"

rm -rf /usr/share/gnome-shell/extensions/tmp

# Release zips and build outputs carry their authors' file modes: unzip kept
# 0600/0770 metadata.json for the quick-settings audio extensions, so
# gnome-shell (running as the user) failed with "Permission denied", and
# tilingshell landed 0777. Make everything readable, drop group/world write,
# and keep existing exec bits (GSConnect launches its daemon directly).
chmod -R u+rwX,go+rX,go-w /usr/share/gnome-shell/extensions

echo "::endgroup::"
