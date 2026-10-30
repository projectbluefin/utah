from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/gnome-extension-shell-versions.py"
SPEC = importlib.util.spec_from_file_location("gnome_extension_shell_versions", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ShellVersionReportTests(unittest.TestCase):
    def test_reads_shell_versions_from_root_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            extension = Path(tmp) / "extension'`"
            extension.mkdir()
            (extension / "metadata.json").write_text('{"shell-version": ["50", "51"]}')
            self.assertEqual(MODULE.shell_versions(extension), "50, 51")

    def test_reads_gsconnect_template_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            extension = Path(tmp) / "extension'`"
            metadata_dir = extension / "data"
            metadata_dir.mkdir(parents=True)
            (metadata_dir / "metadata.json.in").write_text(
                '{"version": "@PACKAGE_VERSION@", "shell-version": ["51"]}'
            )
            self.assertEqual(MODULE.shell_versions(extension), "51")

    def test_uses_verifier_metadata_location_for_nested_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            extension = base / "tmp/tilingshell@ferrarodomenico.com"
            (extension / "resources").mkdir(parents=True)
            (extension / "resources/metadata.json").write_text('{"shell-version": ["51"]}')
            self.assertEqual(MODULE.shell_versions(extension, base), "51")

    def test_reports_each_extension_in_multi_extension_submodule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            extension = base / "tmp/bluefin-bling"
            for name, version in (("syncthing-toggle", "50"), ("power-status-color", "51")):
                (extension / "extensions" / name).mkdir(parents=True)
                (extension / "extensions" / name / "metadata.json").write_text(
                    '{"uuid": "%s@projectbluefin.io", "shell-version": ["%s"]}' % (name, version)
                )
            self.assertEqual(
                MODULE.shell_versions(extension, base),
                "power-status-color@projectbluefin.io: 51; syncthing-toggle@projectbluefin.io: 50",
            )

    def test_every_submodule_maps_to_verifier_metadata(self) -> None:
        paths = subprocess.run(
            ["git", "config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
            cwd=MODULE.REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()[1::2]
        for path in paths:
            with self.subTest(path=path):
                files = MODULE.metadata_files(MODULE.REPO_ROOT / path)
                self.assertTrue(files and all(uuid for uuid, _ in files))

    def test_rejects_untrusted_metadata_for_markdown_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            extension = Path(tmp)
            (extension / "metadata.json").write_text(
                '{"shell-version": ["51|oops", "52`\\nrow"]}'
            )
            with self.assertRaises(ValueError):
                MODULE.shell_versions(extension)

    def test_missing_or_invalid_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            extension = Path(tmp)
            with self.assertRaises(ValueError):
                MODULE.shell_versions(extension)
            (extension / "metadata.json").write_text("not json")
            with self.assertRaises(ValueError):
                MODULE.shell_versions(extension)

    def test_cli_fails_without_report_for_unreadable_metadata(self) -> None:
        for content in (None, b"not json", b"\xff", b"[]",
                        b'{"shell-version": []}', b'{"shell-version": ["51|oops"]}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                if content is not None:
                    (Path(tmp) / "metadata.json").write_bytes(content)
                result = subprocess.run(
                    ["python3", str(SCRIPT), tmp], capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("Cannot propose extension bump:", result.stderr)

    def test_metadata_failure_stops_workflow_before_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["bash", "-c",
                 'set -euo pipefail; { ver=$(python3 "$1" "$2"); '
                 'printf "%s" "$ver"; }; echo proposed',
                 "workflow", str(SCRIPT), tmp],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")

    def test_missing_member_of_multi_extension_submodule_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            extension = base / "tmp/bluefin-bling"
            metadata = extension / "extensions/syncthing-toggle/metadata.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text('{"uuid": "syncthing-toggle@projectbluefin.io", "shell-version": ["51"]}')
            (extension / "extensions/missing-metadata").mkdir()
            with self.assertRaises(ValueError):
                MODULE.shell_versions(extension, base)


if __name__ == "__main__":
    unittest.main()
