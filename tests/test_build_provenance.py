"""Exercise source pin resolution and the file written into an image."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("extension_provenance", ROOT / "scripts/verify-gnome-extensions.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProvenanceTests(unittest.TestCase):
    def test_source_records_cover_gitlinks_and_archive_overrides(self):
        records = MODULE.source_provenance()
        self.assertEqual({r["uuid"] for r in records}, set(MODULE.SOURCE_EXTENSIONS))
        blur = next(r for r in records if r["uuid"] == "blur-my-shell@aunetx")
        self.assertRegex(blur["commit"], r"^[0-9a-f]{40}$")
        for record in records:
            if record["uuid"] in MODULE.ARCHIVES:
                self.assertNotIn("commit", record)
                self.assertRegex(record["archive"]["digest"], r"^sha256:[0-9a-f]{64}$")
        bling = [r for r in records if r["repo"].endswith("/bluefin-bling.git")]
        expected = MODULE.bling_extensions(ROOT / MODULE.EXTENSION_BASE)
        self.assertEqual({r["uuid"] for r in bling}, set(expected))
        self.assertEqual(len({r["commit"] for r in bling}), 1)
        self.assertIn("light-style@projectbluefin.io", expected)

    def test_rejects_checkout_that_differs_from_gitlink(self):
        real = MODULE.subprocess.check_output
        def output(command, **kwargs):
            if command[-2:] == ["rev-parse", "HEAD"]:
                return "0" * 40
            return real(command, **kwargs)
        with patch.object(MODULE.subprocess, "check_output", side_effect=output):
            with self.assertRaisesRegex(ValueError, "checkout does not match gitlink"):
                MODULE.source_provenance()

    def test_image_uses_installed_versions_and_actual_build_arguments(self):
        records = MODULE.source_provenance()
        env = {
            "GNOME_EXTENSION_PROVENANCE": json.dumps(records),
            "SHA_HEAD": "a" * 40, "VERSION": "testing-test", "SOURCE_DATE_EPOCH": "1704067200",
            "BASE_IMAGE": "registry/base@sha256:base", "PACKAGE_IMAGE_REF": "localhost/packages:local",
            "COMMON_IMAGE": "registry/common", "COMMON_IMAGE_SHA": "sha256:common",
            "BREW_IMAGE": "registry/brew", "BREW_IMAGE_SHA": "sha256:brew",
            "WALLPAPERS_IMAGE": "registry/wallpapers", "WALLPAPERS_IMAGE_SHA": "sha256:wallpapers",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env):
            root = Path(tmp)
            for record in records:
                directory = root / "usr/share/gnome-shell/extensions" / record["uuid"]
                directory.mkdir(parents=True)
                (directory / "metadata.json").write_text('{"version": 123}')
            MODULE.record_provenance(root)
            content = (root / "usr/share/utah/provenance.json").read_bytes()
            result = json.loads(content)
            self.assertEqual(result["utah"]["commit"], "a" * 40)
            self.assertEqual(result["base_image"]["digest"], "sha256:base")
            self.assertEqual(result["factory"], {"ref": "localhost/packages:local", "digest": None})
            self.assertTrue(all(r["declared_version"] == 123 for r in result["gnome_extensions"]))
            MODULE.record_provenance(root)
            self.assertEqual(content, (root / "usr/share/utah/provenance.json").read_bytes())
            os.environ["GNOME_EXTENSION_PROVENANCE"] = json.dumps(records[:-1])
            with self.assertRaisesRegex(ValueError, "UUID list differs"):
                MODULE.record_provenance(root)
