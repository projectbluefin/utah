"""Test GNOME extension maintenance guards against GNOME 51 load failures.

Two vendored extensions failed to load on GNOME 51 in a booted image:

- GSConnect subclassed GjsPrivate.DBusImplementation, a final GType since
  GNOME 48, in both its Shell clipboard and its daemon's Wayland clipboard.
  The daemon threw "Cannot inherit from a final type" on every login.
- Dash to Dock v106 imported resource:///org/gnome/shell/ui/pointerWatcher.js,
  which GNOME 51 removed, so the dock never loaded.

These tests read the pinned submodule sources, so a pin moved back to an
affected revision fails here instead of in a booted VM.
"""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = ROOT / "system_files/shared/usr/share/gnome-shell/extensions"
GSCONNECT = EXTENSIONS / "gsconnect@andyholmes.github.io"
DASH_TO_DOCK = EXTENSIONS / "dash-to-dock@micxgx.gmail.com"
FINAL_SUBCLASS = "extends GjsPrivate.DBusImplementation"


class GnomeExtensionPatchTests(unittest.TestCase):
    def test_build_script_guards_the_gsconnect_final_type(self):
        script = (ROOT / "scripts/build-gnome-extensions.sh").read_text()
        self.assertIn(f'grep -rn "{FINAL_SUBCLASS}" "${{gsconnect_dir}}/src"', script)

    def test_gsconnect_does_not_subclass_the_final_type(self):
        # The Shell clipboard, the daemon's Wayland clipboard and the D-Bus
        # utilities all subclassed it before v73; check the whole tree.
        sources = sorted((GSCONNECT / "src").rglob("*.js"))
        if not sources:
            self.skipTest("gsconnect submodule not initialized")
        for path in sources:
            with self.subTest(path=str(path.relative_to(GSCONNECT))):
                self.assertNotIn(FINAL_SUBCLASS, path.read_text(encoding="utf-8"))

    def test_dash_to_dock_does_not_import_the_removed_pointer_watcher(self):
        sources = sorted(DASH_TO_DOCK.rglob("*.js"))
        if not sources:
            self.skipTest("dash-to-dock submodule not initialized")
        # v106 imports the module in dependencies/shell/ui.js and names it only
        # as PointerWatcher in docking.js; match the identifier and the path.
        removed = re.compile(r"\bPointerWatcher\b|ui/pointerWatcher\.js")
        for path in sources:
            with self.subTest(path=path.name):
                self.assertIsNone(removed.search(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
