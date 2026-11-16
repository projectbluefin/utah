"""Exercise source pin resolution and the file written into an image."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("extension_provenance", ROOT / "scripts/verify-gnome-extensions.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fake_rpm(owners):
    """Answer `rpm -qf` for the packaged extension directories."""
    def run(command, **kwargs):
        if command[0] != "rpm":
            raise AssertionError(f"unexpected command {command}")
        uuid = Path(command[-1]).name
        if uuid not in owners:
            return subprocess.CompletedProcess(command, 1, "", f"file {command[-1]} is not owned by any package\n")
        return subprocess.CompletedProcess(command, 0, f"{owners[uuid]} 0:1.0-1.hum1.bfin.noarch\n", "")
    return run


class ProvenanceTests(unittest.TestCase):
    def test_source_records_cover_every_submodule_gitlink(self):
        records = MODULE.source_provenance()
        self.assertEqual({r["uuid"] for r in records}, set(MODULE.SOURCE_EXTENSIONS))
        for record in records:
            self.assertRegex(record["commit"], r"^[0-9a-f]{40}$")

    def test_rejects_checkout_that_differs_from_gitlink(self):
        real = MODULE.subprocess.check_output
        def output(command, **kwargs):
            if command[-2:] == ["rev-parse", "HEAD"]:
                return "0" * 40
            return real(command, **kwargs)
        with patch.object(MODULE.subprocess, "check_output", side_effect=output):
            with self.assertRaisesRegex(ValueError, "checkout does not match gitlink"):
                MODULE.source_provenance()

    def test_packaged_extensions_are_requested_by_the_manifest(self):
        self.assertEqual(MODULE.manifest_errors(ROOT / "packages/utah.toml"), [])
        parity = tomllib.loads((ROOT / "packages/utah.toml").read_text())["factory"]["parity"]
        for package in MODULE.PACKAGED_EXTENSIONS.values():
            with self.subTest(package=package):
                self.assertIn(package, parity)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "utah.toml"
            manifest.write_text('[parity]\npackages = ["gnome-shell-extension-caffeine"]\n')
            errors = MODULE.manifest_errors(manifest)
            self.assertEqual(len(errors), len(MODULE.PACKAGED_EXTENSIONS) - 1)

    def test_no_extension_is_both_built_and_packaged(self):
        self.assertFalse(set(MODULE.SOURCE_EXTENSIONS) & set(MODULE.PACKAGED_EXTENSIONS))

    def test_image_records_submodule_pins_and_package_nevras(self):
        records = MODULE.source_provenance()
        env = {
            "GNOME_EXTENSION_PROVENANCE": json.dumps(records),
            "SHA_HEAD": "a" * 40, "VERSION": "testing-test", "SOURCE_DATE_EPOCH": "1704067200",
            "BASE_IMAGE": "registry/base@sha256:base", "PACKAGE_IMAGE_REF": "localhost/packages:local",
            "COMMON_IMAGE": "registry/common", "COMMON_IMAGE_SHA": "sha256:common",
            "BREW_IMAGE": "registry/brew", "BREW_IMAGE_SHA": "sha256:brew",
            "WALLPAPERS_IMAGE": "registry/wallpapers", "WALLPAPERS_IMAGE_SHA": "sha256:wallpapers",
        }
        rpm = fake_rpm(MODULE.PACKAGED_EXTENSIONS)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env), \
                patch.object(MODULE.subprocess, "run", side_effect=rpm):
            root = Path(tmp)
            for uuid in MODULE.EXTENSIONS:
                directory = root / "usr/share/gnome-shell/extensions" / uuid
                directory.mkdir(parents=True)
                (directory / "metadata.json").write_text('{"version": 123}')
            MODULE.record_provenance(root)
            content = (root / "usr/share/utah/provenance.json").read_bytes()
            result = json.loads(content)
            self.assertEqual(result["utah"]["commit"], "a" * 40)
            self.assertEqual(result["base_image"]["digest"], "sha256:base")
            self.assertEqual(result["factory"], {"ref": "localhost/packages:local", "digest": None})
            self.assertEqual([r["uuid"] for r in result["gnome_extensions"]], MODULE.EXTENSIONS)
            self.assertTrue(all(r["declared_version"] == 123 for r in result["gnome_extensions"]))
            caffeine = next(r for r in result["gnome_extensions"] if r["uuid"] == "caffeine@patapon.info")
            self.assertEqual(caffeine["nevra"], "gnome-shell-extension-caffeine-0:1.0-1.hum1.bfin.noarch")
            MODULE.record_provenance(root)
            self.assertEqual(content, (root / "usr/share/utah/provenance.json").read_bytes())
            os.environ["GNOME_EXTENSION_PROVENANCE"] = json.dumps(records[:-1])
            with self.assertRaisesRegex(ValueError, "UUID list differs"):
                MODULE.record_provenance(root)

    def test_an_extension_another_package_owns_fails_the_record(self):
        owners = dict(MODULE.PACKAGED_EXTENSIONS, **{"caffeine@patapon.info": "something-else"})
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(MODULE.subprocess, "run", side_effect=fake_rpm(owners)):
            with self.assertRaisesRegex(ValueError, "expected gnome-shell-extension-caffeine to own"):
                MODULE.packaged_provenance(Path(tmp))
        owners.pop("caffeine@patapon.info")
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(MODULE.subprocess, "run", side_effect=fake_rpm(owners)):
            with self.assertRaisesRegex(ValueError, "not owned by any package"):
                MODULE.packaged_provenance(Path(tmp))
