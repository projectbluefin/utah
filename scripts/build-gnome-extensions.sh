#!/usr/bin/bash

set -eoux pipefail

echo "::group:: ===$(basename "$0")==="

# Build Extensions
# Default enablement is supplied by common's GNOME schema override. It must
# include each extension built below that Bluefin smoke verifies; CI preserves it.
# Extension schemas are validated and installed globally at the end of this
# script, not compiled per extension.

# AppIndicator Support runs from its source tree; nothing to build.

# Bazaar Companion
mv /usr/share/gnome-shell/extensions/tmp/bazaar-integration@kolunmi.github.io/src/ /usr/share/gnome-shell/extensions/bazaar-integration@kolunmi.github.io/

# Blur My Shell
make -C /usr/share/gnome-shell/extensions/blur-my-shell@aunetx
unzip -o /usr/share/gnome-shell/extensions/blur-my-shell@aunetx/build/blur-my-shell@aunetx.shell-extension.zip -d /usr/share/gnome-shell/extensions/blur-my-shell@aunetx
rm -rf /usr/share/gnome-shell/extensions/blur-my-shell@aunetx/build

# Caffeine
# The Caffeine extension is built/packaged into a temporary subdirectory (tmp/caffeine/caffeine@patapon.info).
# Unlike other extensions, it must be moved to the standard extensions directory so GNOME Shell can detect it.
mv /usr/share/gnome-shell/extensions/tmp/caffeine/caffeine@patapon.info /usr/share/gnome-shell/extensions/caffeine@patapon.info

# Dash to Dock
make -C /usr/share/gnome-shell/extensions/dash-to-dock@micxgx.gmail.com

# Gradia Capture
bash /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io/build.sh
unzip -o /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io/gradia-integration@alexandervanhee.github.io.shell-extension.zip -d /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io
rm -f /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io/gradia-integration@alexandervanhee.github.io.shell-extension.zip

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

# Custom Command Menu and BudsLink Companion run from their source trees.

# Tailscale Quick Settings
mv /usr/share/gnome-shell/extensions/tmp/tailscale-gnome-qs/tailscale-gnome-qs@tailscale-qs.github.io /usr/share/gnome-shell/extensions/tailscale-gnome-qs@tailscale-qs.github.io

# Syncthing Toggle (rehhouari): the upstream toggle Bluefin smoke checks.
# Bling's syncthing-toggle@projectbluefin.io below declares the same schema id
# (org.gnome.shell.extensions.syncthing-toggle) with a superset of these keys,
# and only one file per id may be installed globally, so drop this copy and
# let Bling's schema serve both.
syncthing_toggle_dir="/usr/share/gnome-shell/extensions/syncthing-toggle@rehhouari.github.com"
mv -T /usr/share/gnome-shell/extensions/tmp/syncthing-toggle-rehhouari "${syncthing_toggle_dir}"
rm -rf "${syncthing_toggle_dir}/schemas" "${syncthing_toggle_dir}/.git"
# Bluefin calls this control "Sync Folder" (Bling's toggle, Dakota's docs and
# the Bluefin documentation's Sync Folder page), and Bluefin smoke checks the
# pill and menu header carry that label. Upstream labels both "Syncthing".
# Rename exactly those two strings and fail if upstream moved them.
sed -i "s/title: _('Syncthing'),/title: _('Sync Folder'),/; s/setHeader(syncthingIcon, _('Syncthing'))/setHeader(syncthingIcon, _('Sync Folder'))/" \
    "${syncthing_toggle_dir}/toggle.js"
test "$(grep -c "_('Sync Folder')" "${syncthing_toggle_dir}/toggle.js")" -eq 2
if grep -n "_('Syncthing')" "${syncthing_toggle_dir}/toggle.js"; then
    echo "syncthing-toggle@rehhouari.github.com still labels a control Syncthing" >&2
    exit 1
fi

# Bluefin Bling: install every upstream extension, without enabling any.
# Keep UUID validation aligned with bling_extensions in the source verifier.
for extension in /usr/share/gnome-shell/extensions/tmp/bluefin-bling/extensions/*/; do
    uuid=$(jq -er '.uuid | strings | select(test("\\A[A-Za-z0-9_.+-]+@[A-Za-z0-9_.-]+\\z"))' "${extension}/metadata.json")
    destination="/usr/share/gnome-shell/extensions/${uuid}"
    test ! -e "${destination}"
    mv -T "${extension}" "${destination}"
done

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

# Quick Settings Audio Panel
qsap_zip="/tmp/quick-settings-audio-panel.zip"
qsap_sha256="3a6da80e9b391251149bcf87a9cb5dbbccee827887d1aef43be4ee090b04a29f"
qsap_dir="/usr/share/gnome-shell/extensions/quick-settings-audio-panel@rayzeq.github.io"
curl -fsSL "https://github.com/Rayzeq/quick-settings-audio-panel/releases/download/v103/quick-settings-audio-panel%40rayzeq.github.io.shell-extension.zip" -o "${qsap_zip}"
echo "${qsap_sha256}  ${qsap_zip}" | sha256sum --check --strict
mkdir -p "${qsap_dir}"
unzip -o "${qsap_zip}" -d "${qsap_dir}"
rm -f "${qsap_zip}"

# Quick Settings Audio Devices Hider
hider_zip="/tmp/quicksettings-audio-devices-hider.zip"
hider_sha256="217082e049572ae54bc90bc09240f896e25f4a2609e0c0aa4b7133bed46d935d"
hider_dir="/usr/share/gnome-shell/extensions/quicksettings-audio-devices-hider@marcinjahn.com"
curl -fsSL "https://extensions.gnome.org/download-extension/quicksettings-audio-devices-hider@marcinjahn.com.shell-extension.zip?version_tag=69924" -o "${hider_zip}"
echo "${hider_sha256}  ${hider_zip}" | sha256sum --check --strict
mkdir -p "${hider_dir}"
unzip -o "${hider_zip}" -d "${hider_dir}"
rm -f "${hider_zip}"

# Quick Settings Audio Devices Renamer
renamer_zip="/tmp/quicksettings-audio-devices-renamer.zip"
renamer_sha256="a4f33897983376a23c5de69d6a008d1451afcc05cd7ce3b56630bd739c095a2e"
renamer_dir="/usr/share/gnome-shell/extensions/quicksettings-audio-devices-renamer@marcinjahn.com"
curl -fsSL "https://extensions.gnome.org/download-extension/quicksettings-audio-devices-renamer@marcinjahn.com.shell-extension.zip?version_tag=69925" -o "${renamer_zip}"
echo "${renamer_sha256}  ${renamer_zip}" | sha256sum --check --strict
mkdir -p "${renamer_dir}"
unzip -o "${renamer_zip}" -d "${renamer_dir}"
rm -f "${renamer_zip}"

# Copyous clipboard manager (Gda/libgda-sqlite come from packages/bluefin.toml)
copyous_zip="/tmp/copyous@boerdereinar.dev.zip"
copyous_sha256="e4af596ffeab8137c90b0c178b44160e9fc4dfb1296fb5573ae36871e485a90a"
copyous_dir="/usr/share/gnome-shell/extensions/copyous@boerdereinar.dev"
curl -fsSL "https://github.com/boerdereinar/copyous/releases/download/v2.1.2/copyous@boerdereinar.dev.zip" -o "${copyous_zip}"
echo "${copyous_sha256}  ${copyous_zip}" | sha256sum --check --strict
mkdir -p "${copyous_dir}"
unzip -o "${copyous_zip}" -d "${copyous_dir}"
rm -f "${copyous_zip}"

rm -rf /usr/share/gnome-shell/extensions/tmp

# Extension schemas are global, the way Fedora's extension RPMs install them.
# Left in <uuid>/schemas/, a schema is invisible to the image-wide
# glib-compile-schemas the Containerfile runs next, and glib then drops every
# override section naming it without a word; Extension.getSettings() would also
# prefer the local compiled copy over the global one. That is how common's
# [org.gnome.shell.extensions.dash-to-dock] disable-overview-on-startup=true
# never applied and GNOME opened the Activities overview at login. Validate
# each extension's schemas strictly, install them into the system directory
# (never over an existing file), and remove the extension-local directory with
# its compiled cache. GSConnect's meson install already put its schema there.
# glib-compile-schemas also ignores, with exit 0, a second file declaring a
# schema id; utah-verify-desktop-contract fails the build on duplicate ids and
# on override sections that still name no installed schema.
system_schemas=/usr/share/glib-2.0/schemas
for schemas in /usr/share/gnome-shell/extensions/*/schemas/; do
    [ -d "${schemas}" ] || continue
    glib-compile-schemas --strict --dry-run "${schemas}"
    shopt -s nullglob
    schema_files=("${schemas}"*.gschema.xml)
    shopt -u nullglob
    if [ "${#schema_files[@]}" -eq 0 ]; then
        echo "${schemas} has no .gschema.xml to install globally" >&2
        exit 1
    fi
    for schema in "${schema_files[@]}"; do
        if [ -e "${system_schemas}/${schema##*/}" ]; then
            echo "${schema##*/} is already installed in ${system_schemas}" >&2
            exit 1
        fi
        install -m 0644 "${schema}" "${system_schemas}/"
    done
    rm -rf "${schemas}"
done

# Release zips and build outputs carry their authors' file modes: unzip kept
# 0600/0770 metadata.json for the quick-settings audio extensions, so
# gnome-shell (running as the user) failed with "Permission denied", and
# tilingshell landed 0777. Make everything readable, drop group/world write,
# and keep existing exec bits (GSConnect launches its daemon directly).
chmod -R u+rwX,go+rX,go-w /usr/share/gnome-shell/extensions

echo "::endgroup::"
