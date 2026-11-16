"""Test GNOME extension maintenance guards against GNOME 51 load failures.

GSConnect, the one extension still built from a pinned submodule, subclassed
GjsPrivate.DBusImplementation, a final GType since GNOME 48, in both its Shell
clipboard and its daemon's Wayland clipboard. The daemon threw "Cannot inherit
from a final type" on every login. These tests read the pinned submodule
source, so a pin moved back to an affected revision fails here instead of in a
booted VM.

Extension schemas must also be global: left in an extension's schemas/, the
image-wide glib-compile-schemas dropped common's dash-to-dock override and
GNOME opened the Activities overview at login. And extension defaults in the
distro dconf database only apply under a schema's declared path=.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = ROOT / "system_files/shared/usr/share/gnome-shell/extensions"
GSCONNECT = EXTENSIONS / "gsconnect@andyholmes.github.io"
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

    def test_only_unpackaged_extensions_are_submodules(self):
        # Every other extension is a factory RPM (#1235); a submodule for one
        # of them would install a second copy over the package's files.
        gitmodules = (ROOT / ".gitmodules").read_text()
        self.assertIn("gsconnect@andyholmes.github.io", gitmodules)
        self.assertEqual(gitmodules.count("[submodule "), 1)


class InstalledExtensionTests(unittest.TestCase):
    """The installed verifier rejects extension-local schemas/ and extension
    dconf defaults that no installed schema path declares."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "image"
        self.script = self.root / "libexec/verify.py"
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/verify-gnome-extensions.py", self.script)
        spec = importlib.util.spec_from_file_location("installed_verifier", self.script)
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        self.extensions = self.root / "usr/share/gnome-shell/extensions"
        for uuid in verifier.EXTENSIONS:
            (self.extensions / uuid).mkdir(parents=True)
            (self.extensions / uuid / "metadata.json").write_text('{"shell-version": ["51"]}')
        self.schemas = self.root / "usr/share/glib-2.0/schemas"
        self.schemas.mkdir(parents=True)
        (self.schemas / "org.gnome.shell.extensions.tilingshell.gschema.xml").write_text(
            '<schemalist><schema id="org.gnome.shell.extensions.tilingshell"'
            ' path="/org/gnome/shell/extensions/tilingshell/"></schema></schemalist>'
        )
        self.distro = self.root / "etc/dconf/db/distro.d"
        (self.distro / "locks").mkdir(parents=True)

    def verify(self):
        command = ["python3", str(self.script), "--root", str(self.root)]
        return subprocess.run(command, env=dict(os.environ), capture_output=True, text=True)

    def test_a_complete_tree_passes(self):
        (self.distro / "08-defaults").write_text(
            "[org/gnome/shell/extensions/tilingshell]\nlayouts-json='[{\"id\": \"%\"}]'\n"
            "[org/gnome/desktop/interface]\nclock-format='24h'\n"
        )
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installed_verifier_rejects_extension_local_schemas(self):
        schemas = self.extensions / "dash-to-dock@micxgx.gmail.com/schemas"
        schemas.mkdir()
        (schemas / "gschemas.compiled").write_text("")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{schemas}: extension-local schemas mask the global ones", result.stderr)

    def test_extension_defaults_must_name_an_installed_schema_path(self):
        keyfile = self.distro / "08-defaults"
        keyfile.write_text("[org/gnome/shell/extensions/tilingshell@ferrarodomenico.com]\nshow-indicator=false\n")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("[org/gnome/shell/extensions/tilingshell@ferrarodomenico.com] matches no installed schema path", result.stderr)

    def test_a_missing_packaged_extension_fails(self):
        shutil.rmtree(self.extensions / "power-status-color@projectbluefin.io")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("power-status-color@projectbluefin.io: missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
