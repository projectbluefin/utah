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

# GNOME 48+ makes GjsPrivate.DBusImplementation a final GType, so
# GObject.registerClass(... extends GjsPrivate.DBusImplementation) throws
# "Cannot inherit from a final type" at module load, which takes the whole
# GSConnect extension down. Wrap the registration in try/catch so a final base
# type degrades to an inert portal instead of a hard load failure. Fail loudly
# if the upstream lines move so a submodule bump cannot silently drop the fix.
gs_clipboard="${gsconnect_dir}/src/shell/clipboard.js"
python3 - "${gs_clipboard}" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
content = path.read_text(encoding="utf-8")

old_start = "export const Clipboard = GObject.registerClass({"
new_start = """let Clipboard;
try {
Clipboard = GObject.registerClass({"""

old_end = """        if (this._handleMethodCallId > 0) {
            this.disconnect(this._handleMethodCallId);
            this._handleMethodCallId = 0;
            this.unexport();
        }
    }
});"""

new_end = """        if (this._handleMethodCallId > 0) {
            this.disconnect(this._handleMethodCallId);
            this._handleMethodCallId = 0;
            this.unexport();
        }
    }
});
} catch (_e) {
    // GNOME 48+ makes GjsPrivate.DBusImplementation a final GType, so the
    // Wayland background clipboard portal cannot be registered as a
    // subclass of it: registerClass throws "Cannot inherit from a final
    // type" at module load, which otherwise takes the whole extension down.
    // Fall back to an inert portal so the rest of GSConnect still loads;
    // clipboard sync through this portal is disabled on such shells.
    console.warn(`GSConnect: clipboard portal disabled on this GNOME version: ${_e}`);
    Clipboard = class { destroy() {} };
}
export { Clipboard };"""

if old_start not in content or old_end not in content:
    sys.exit("gsconnect clipboard.js no longer matches expected DBusImplementation class structure; re-check the GNOME 51 guard")

content = content.replace(old_start, new_start, 1)
content = content.replace(old_end, new_end, 1)
path.write_text(content, encoding="utf-8")
PY

meson setup --prefix=/usr "${gsconnect_dir}" "${gsconnect_dir}/_build"
meson install -C "${gsconnect_dir}/_build" --skip-subprojects
# GSConnect installs schemas to /usr/share/glib-2.0/schemas and meson compiles them automatically

# Assert the installed extension carries the guard
test -f "${gsconnect_dir}/shell/clipboard.js"
if ! grep -q "clipboard portal disabled on this GNOME version" "${gsconnect_dir}/shell/clipboard.js"; then
    echo "gsconnect shell/clipboard.js was not installed with the GNOME 51 final-type guard" >&2
    exit 1
fi

# Custom Command Menu
glib-compile-schemas --strict /usr/share/gnome-shell/extensions/custom-command-list@storageb.github.com/schemas

rm -rf /usr/share/gnome-shell/extensions/tmp

echo "::endgroup::"
