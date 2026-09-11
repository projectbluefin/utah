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
# Upstream's Makefile renders stylesheet.css from _stylesheet.scss with sassc,
# which neither the Hummingbird base nor the Utah package repository provides
# (see [unavailable] in packages/utah.toml). The stylesheet is deterministic
# for the pinned submodule ref, so Utah ships it precompiled. Guard on the
# scss digest so a submodule bump cannot silently ship a stale stylesheet.
dtd_dir="/usr/share/gnome-shell/extensions/dash-to-dock@micxgx.gmail.com"
dtd_scss_sha="75892eff2b2d98fc94046a1d1a0a0defb4daaa8f0cadee251205e353ea048e0f"
if ! echo "${dtd_scss_sha}  ${dtd_dir}/_stylesheet.scss" | sha256sum --check --strict --status; then
    echo "dash-to-dock _stylesheet.scss no longer matches the precompiled stylesheet; re-render system_files/shared/usr/share/utah/dash-to-dock-stylesheet.css" >&2
    exit 1
fi
install -m 0644 /usr/share/utah/dash-to-dock-stylesheet.css "${dtd_dir}/stylesheet.css"
glib-compile-schemas --strict "${dtd_dir}/schemas"

# Gradia Capture
bash /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io/build.sh
unzip -o /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io/gradia-integration@alexandervanhee.github.io.shell-extension.zip -d /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io
rm -f /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io/gradia-integration@alexandervanhee.github.io.shell-extension.zip
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/gradia-integration@alexandervanhee.github.io/schemas

# GSConnect
# Disable update-desktop-database hook since desktop-file-utils is unavailable in Hummingbird/Utah repositories.
gsconnect_dir="/usr/share/gnome-shell/extensions/gsconnect@andyholmes.github.io"
sed -i 's/update_desktop_database: true/update_desktop_database: false/' "${gsconnect_dir}/meson.build"
meson setup --prefix=/usr "${gsconnect_dir}" "${gsconnect_dir}/_build"
meson install -C "${gsconnect_dir}/_build" --skip-subprojects
# GSConnect installs schemas to /usr/share/glib-2.0/schemas and meson compiles them automatically

# Custom Command Menu
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/custom-command-list@storageb.github.com/schemas

# Search Light
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/search-light@icedman.github.com/schemas

rm -rf /usr/share/gnome-shell/extensions/tmp

echo "::endgroup::"
