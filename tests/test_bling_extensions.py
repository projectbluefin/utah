"""Bling additions must ship automatically, but must never enable themselves."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify-gnome-extensions.py"
SPEC = importlib.util.spec_from_file_location("bling_verifier", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BlingTests(unittest.TestCase):
    def member(self, base, name, uuid):
        folder = base / MODULE.BLING_BASE / name
        folder.mkdir(parents=True)
        (folder / "metadata.json").write_text(json.dumps({"uuid": uuid, "shell-version": ["51"]}))
        return folder

    def test_every_checked_out_folder_has_valid_unique_uuid(self):
        base = ROOT / MODULE.EXTENSION_BASE
        found = MODULE.bling_extensions(base)
        folders = [p for p in (base / MODULE.BLING_BASE).iterdir() if p.is_dir()]
        self.assertEqual(len(found), len(folders))
        self.assertIn("light-style@projectbluefin.io", found)
        self.assertTrue(found.items() <= MODULE.SOURCE_EXTENSIONS.items())

    def test_new_extension_needs_no_list_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.member(base, "future", "future@example.org")
            self.assertEqual(MODULE.bling_extensions(base), {
                "future@example.org": "tmp/bluefin-bling/extensions/future/metadata.json",
            })

    def test_rejects_missing_invalid_and_duplicate_metadata(self):
        for uuid in (None, "", 42, "../escape", "bad|row@example.org", "a/b@example.org", "a@example.org\n"):
            with self.subTest(uuid=uuid), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                folder = self.member(base, "invalid", uuid)
                with self.assertRaises(ValueError):
                    MODULE.bling_extensions(base)
                (folder / "metadata.json").unlink()
                with self.assertRaises(ValueError):
                    MODULE.bling_extensions(base)
                (folder / "metadata.json").write_text("not json")
                with self.assertRaises(ValueError):
                    MODULE.bling_extensions(base)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self.assertRaises(ValueError):
                MODULE.bling_extensions(base)
            self.member(base, "one", "same@example.org")
            self.member(base, "two", "same@example.org")
            with self.assertRaisesRegex(ValueError, "duplicate uuid"):
                MODULE.bling_extensions(base)

    def build_loop(self, base):
        build = (ROOT / "scripts/build-gnome-extensions.sh").read_text()
        loop = build.split("# Bluefin Bling:", 1)[1].split("# Curated release", 1)[0]
        return loop[loop.index("for extension"):].replace("/usr/share/gnome-shell/extensions", str(base))

    @unittest.skipUnless(shutil.which("jq"), "jq required for build loop")
    def test_build_loop_moves_all_members_and_compiles_only_present_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            folder = self.member(base, "future", "future@example.org")
            (folder / "schemas").mkdir()
            self.member(base, "no-schema", "other@example.org")
            loop = self.build_loop(base)
            result = subprocess.run([
                "bash", "-euo", "pipefail", "-c",
                'glib-compile-schemas() { printf "compiled:%s\\n" "$*"; };\n' + loop,
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"compiled:--strict {base}/future@example.org/schemas\n")
            for uuid in ("future@example.org", "other@example.org"):
                self.assertTrue((base / uuid / "metadata.json").is_file())
            self.assertFalse(folder.exists())
            self.assertNotIn("enabled-extensions", loop)

    @unittest.skipUnless(shutil.which("jq"), "jq required for build loop")
    def test_build_loop_rejects_invalid_uuid_and_destination_collisions(self):
        for uuid in (None, 42, "../escape", "bad|row@example.org", "a@example.org\n", "existing@example.org"):
            with self.subTest(uuid=uuid), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                folder = self.member(base, "invalid", uuid)
                (base / "existing@example.org").mkdir()
                result = subprocess.run([
                    "bash", "-euo", "pipefail", "-c", self.build_loop(base),
                ], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(folder.is_dir())

    def test_installed_verifier_uses_provenance_after_sources_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "libexec/verify.py"
            script.parent.mkdir()
            shutil.copyfile(SCRIPT, script)
            records = [{"uuid": uuid} for uuid in MODULE.SOURCE_EXTENSIONS]
            for record in records:
                folder = root / "usr/share/gnome-shell/extensions" / record["uuid"]
                folder.mkdir(parents=True)
                (folder / "metadata.json").write_text('{"shell-version": ["51"]}')
            provenance = root / "usr/share/utah/provenance.json"
            provenance.parent.mkdir()
            provenance.write_text(json.dumps({"gnome_extensions": records}))
            env = dict(os.environ)
            env.pop("GNOME_EXTENSION_PROVENANCE", None)
            for build_env in (False, True):
                if build_env:
                    env["GNOME_EXTENSION_PROVENANCE"] = json.dumps(records)
                command = ["python3", str(script), "--root", str(root)]
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            missing = root / "usr/share/gnome-shell/extensions/light-style@projectbluefin.io/metadata.json"
            missing.unlink()
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("light-style@projectbluefin.io: missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
