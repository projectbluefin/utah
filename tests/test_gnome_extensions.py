"""Test GNOME extension maintenance guards against GNOME 51 load failures.

Two vendored extensions failed to load on GNOME 51 in a booted image:

- GSConnect subclassed GjsPrivate.DBusImplementation, a final GType since
  GNOME 48, in both its Shell clipboard and its daemon's Wayland clipboard.
  The daemon threw "Cannot inherit from a final type" on every login.
- Dash to Dock v106 imported resource:///org/gnome/shell/ui/pointerWatcher.js,
  which GNOME 51 removed, so the dock never loaded.

These tests read the pinned submodule sources, so a pin moved back to an
affected revision fails here instead of in a booted VM.

Extension schemas must also be global: left in an extension's schemas/, the
image-wide glib-compile-schemas dropped common's dash-to-dock override and
GNOME opened the Activities overview at login.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = ROOT / "system_files/shared/usr/share/gnome-shell/extensions"
GSCONNECT = EXTENSIONS / "gsconnect@andyholmes.github.io"
DASH_TO_DOCK = EXTENSIONS / "dash-to-dock@micxgx.gmail.com"
FINAL_SUBCLASS = "extends GjsPrivate.DBusImplementation"


class GnomeExtensionPatchTests(unittest.TestCase):
    def test_extension_state_helpers_follow_last_state_and_accept_active_states(self):
        helper = ROOT / "iso/scripts/extension-state.sh"
        cases = (
            ("State: ACTIVE\n", "ACTIVE", True),
            ("State: ENABLED\n", "ENABLED", True),
            ("State: ERROR\n", "ERROR", False),
            ("State: ACTIVE\nState: ERROR\n", "ERROR", False),
            ("no state\n", "", False),
        )
        command = (
            'source "$1"; '
            'state="$(extension_state_from_info)"; '
            'printf "%s\\n" "$state"; '
            'extension_state_is_active "$state"'
        )
        for info, expected_state, expected_active in cases:
            with self.subTest(info=info):
                result = subprocess.run(
                    ["bash", "-c", command, "extension-state-test", str(helper)],
                    input=info,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.stdout.rstrip("\n"), expected_state)
                self.assertEqual(result.returncode, 0 if expected_active else 1)

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


class ExtensionSchemaTests(unittest.TestCase):
    """The build installs every extension schema globally and the installed
    verifier rejects any extension-local schemas/ left behind."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.extensions = self.tmp / "extensions"
        self.system = self.tmp / "schemas"
        self.system.mkdir()
        self.log = self.tmp / "compile.log"

    def extension(self, uuid, *files):
        folder = self.extensions / uuid
        folder.mkdir(parents=True)
        (folder / "metadata.json").write_text("{}")
        for name in files:
            path = folder / "schemas" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(name)
        return folder

    def install_schemas(self, compile_status=0):
        script = (ROOT / "scripts/build-gnome-extensions.sh").read_text()
        start = script.index("system_schemas=")
        block = script[start:script.index("\ndone\n", start) + len("\ndone\n")]
        block = block.replace("/usr/share/gnome-shell/extensions", str(self.extensions))
        block = block.replace("/usr/share/glib-2.0/schemas", str(self.system))
        stub = f'glib-compile-schemas() {{ printf "%s\\n" "$*" >> "{self.log}"; return {compile_status}; }}\n'
        return subprocess.run(["bash", "-euo", "pipefail", "-c", stub + block], capture_output=True, text=True)

    def test_schemas_are_validated_installed_globally_and_removed_locally(self):
        dock = self.extension("dock@example.org", "org.example.dock.gschema.xml", "gschemas.compiled", "meson.build")
        plain = self.extension("plain@example.org")
        result = self.install_schemas()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(), f"--strict --dry-run {dock}/schemas/\n")
        installed = self.system / "org.example.dock.gschema.xml"
        self.assertEqual(installed.read_text(), "org.example.dock.gschema.xml")
        self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(path.name for path in self.system.iterdir()), ["org.example.dock.gschema.xml"])
        self.assertFalse((dock / "schemas").exists())
        self.assertTrue((plain / "metadata.json").is_file())

    def test_an_existing_global_schema_is_never_overwritten(self):
        self.extension("dock@example.org", "org.example.dock.gschema.xml")
        (self.system / "org.example.dock.gschema.xml").write_text("system")
        result = self.install_schemas()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("org.example.dock.gschema.xml is already installed", result.stderr)
        self.assertEqual((self.system / "org.example.dock.gschema.xml").read_text(), "system")

    def test_a_compiled_only_schema_directory_fails(self):
        self.extension("dock@example.org", "gschemas.compiled")
        result = self.install_schemas()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no .gschema.xml to install globally", result.stderr)

    def test_an_invalid_schema_fails_before_anything_is_installed(self):
        dock = self.extension("dock@example.org", "org.example.dock.gschema.xml")
        result = self.install_schemas(compile_status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.system.iterdir()), [])
        self.assertTrue((dock / "schemas").is_dir())

    def test_installed_verifier_rejects_extension_local_schemas(self):
        root = self.tmp / "image"
        script = root / "libexec/verify.py"
        script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/verify-gnome-extensions.py", script)
        spec = importlib.util.spec_from_file_location("installed_verifier", script)
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        extensions = root / "usr/share/gnome-shell/extensions"
        for uuid in verifier.SOURCE_EXTENSIONS:
            (extensions / uuid).mkdir(parents=True)
            (extensions / uuid / "metadata.json").write_text('{"shell-version": ["51"]}')
        records = [{"uuid": uuid} for uuid in verifier.SOURCE_EXTENSIONS]
        env = dict(os.environ, GNOME_EXTENSION_PROVENANCE=json.dumps(records))
        command = ["python3", str(script), "--root", str(root)]
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        schemas = extensions / "dash-to-dock@micxgx.gmail.com/schemas"
        schemas.mkdir()
        (schemas / "gschemas.compiled").write_text("")
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{schemas}: extension-local schemas mask the global ones", result.stderr)


if __name__ == "__main__":
    unittest.main()
