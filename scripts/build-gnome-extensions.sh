#!/usr/bin/bash

set -eoux pipefail

echo "::group:: ===$(basename "$0")==="

# Build the GNOME extensions that are still pinned submodules.
# Dakota's other fifteen extensions are factory RPMs installed by the package
# transaction (packages/utah.toml [parity], #1235); they install their schemas
# into /usr/share/glib-2.0/schemas and ship their own file modes, so nothing
# here touches them. Default enablement is the zz3 schema override.

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
# GSConnect installs its schema to /usr/share/glib-2.0/schemas, where the
# Containerfile's image-wide glib-compile-schemas sees it; the installed
# verifier fails on any extension-local schemas/ directory.

# The submodule tree arrives with the checkout's file modes, which are
# group-writable under a 002 umask. Make it readable, drop group/world write,
# and keep existing exec bits (GSConnect launches its daemon directly).
chmod -R u+rwX,go+rX,go-w "${gsconnect_dir}"

echo "::endgroup::"
