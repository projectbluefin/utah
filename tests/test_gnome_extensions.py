"""Test GNOME extension maintenance guards and patches."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class GnomeExtensionPatchTests(unittest.TestCase):
    def test_build_script_contains_gsconnect_final_type_guard(self):
        script = (ROOT / "scripts/build-gnome-extensions.sh").read_text()
        self.assertIn("GjsPrivate.DBusImplementation a final GType", script)
        self.assertIn("clipboard portal disabled on this GNOME version", script)
        self.assertIn("Clipboard = class { destroy() {} };", script)

    def test_gsconnect_clipboard_patch_applies_and_validates(self):
        src_clipboard = (
            ROOT
            / "system_files/shared/usr/share/gnome-shell/extensions/gsconnect@andyholmes.github.io/src/shell/clipboard.js"
        )
        if not src_clipboard.is_file():
            self.skipTest("gsconnect submodule not initialized")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_clipboard = Path(tmp) / "clipboard.js"
            shutil.copyfile(src_clipboard, tmp_clipboard)

            # Extract python patch snippet from build-gnome-extensions.sh
            script = (ROOT / "scripts/build-gnome-extensions.sh").read_text()
            self.assertIn("python3 - \"${gs_clipboard}\" <<'PY'", script)
            py_code = script.split("python3 - \"${gs_clipboard}\" <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

            proc = subprocess.run(
                ["python3", "-c", py_code, str(tmp_clipboard)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"patch script failed: {proc.stderr}")

            patched_text = tmp_clipboard.read_text(encoding="utf-8")
            self.assertIn("clipboard portal disabled on this GNOME version", patched_text)
            self.assertIn("Clipboard = class { destroy() {} };", patched_text)
            self.assertIn("export { Clipboard };", patched_text)

            # If node is present, check syntax
            if shutil.which("node"):
                res = subprocess.run(
                    ["node", "--check", str(tmp_clipboard)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(res.returncode, 0, f"syntax error in patched clipboard.js: {res.stderr}")


if __name__ == "__main__":
    unittest.main()
