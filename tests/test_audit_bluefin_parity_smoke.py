"""Smoke test: drive scripts/audit-bluefin-parity.py against fake repodata.

This is the human-noticed contract: an operator runs `just
audit-bluefin-parity`, sees three partition counts, and either agrees
with the verdict or files an issue. The offline smoke below mocks the
factory OCI and Hummingbird HTTP pulls to confirm the partition logic
prints what the README and the issue promise.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit-bluefin-parity.py"

spec = importlib.util.spec_from_file_location("audit_bluefin_parity_smoke", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def primary_xml(names: dict[str, str]) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<metadata xmlns="http://linux.duke.edu/metadata/rpm" packages="' + str(len(names)) + '">',
    ]
    for name, evr in names.items():
        arch = evr.rsplit(".", 1)[-1] if "." in evr else "x86_64"
        body = evr[: -len("." + arch)] if "." in evr else evr
        if ":" in body:
            epoch, vr = body.split(":", 1)
        else:
            epoch, vr = "0", body
        version, release = vr.rsplit("-", 1) if "-" in vr else (vr, "0")
        lines.append(
            f'<package type="rpm"><name>{name}</name><arch>{arch}</arch>'
            f'<version epoch="{epoch}">{version}</version>'
            f'<release>{release}</release></package>'
        )
    lines.append("</metadata>")
    return "\n".join(lines).encode()


def repomd_xml() -> bytes:
    return textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <repomd xmlns="http://linux.duke.edu/metadata/repo">
          <data type="primary">
            <location href="repodata/primary.xml"/>
            <checksum type="sha256">unused</checksum>
          </data>
        </repomd>
        """).encode()


class AuditSmokeTests(unittest.TestCase):
    """Drive cmd_run end-to-end with HTTP pulled from a tmpdir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # Fake factory OCI metadata: a tar.gz whose root looks like the
        # real one (/repository/repodata/repomd.xml + primary.xml).
        self.factory_dir = self.root / "factory"
        (self.factory_dir / "repository/repodata").mkdir(parents=True)
        (self.factory_dir / "repository/repodata/repomd.xml").write_bytes(repomd_xml())
        (self.factory_dir / "repository/repodata/primary.xml").write_bytes(primary_xml({
            "intel-gmmlib": "22.5.4-1.bfin.x86_64",
            "libgda": "6.0.0-1.bfin.x86_64",
            "libva-intel-media-driver": "24.4.0-1.bfin.x86_64",
        }))
        # The script unpacks to destination/repodata/, not destination/. So
        # mock_factory_metadata writes to a "repodata" subdir.
        self.unpacked_factory = self.root / "factory_unpacked"
        (self.unpacked_factory / "repodata").mkdir(parents=True)
        (self.unpacked_factory / "repodata/repomd.xml").write_bytes(repomd_xml())
        (self.unpacked_factory / "repodata/primary.xml").write_bytes(
            (self.factory_dir / "repository/repodata/primary.xml").read_bytes())

        # Fake Hummingbird primary.xml.
        (self.root / "hummingbird-primary.xml").write_bytes(primary_xml({
            "wpa_supplicant": "2.11-1.fc44.x86_64",
            "wireless-regdb": "20240918-1.fc44.x86_64",
            "iw": "6.9-1.fc44.x86_64",
        }))

        # Fake Containerfile with the right pins.
        (self.root / "Containerfile").write_text(
            "ARG BASE_IMAGE=ghcr.io/example/base@sha256:" + "d" * 64 + "\n"
            "ARG PACKAGE_IMAGE=ghcr.io/example/packages\n"
            "ARG PACKAGE_IMAGE_SHA=sha256:" + "f" * 64 + "\n"
        )

        # Fake Bluefin manifest with a representative slice.
        (self.root / "bluefin.toml").write_text(textwrap.dedent("""\
            [fedora]
            packages = ["wpa_supplicant", "wireless-regdb", "iw",
                        "intel-gmmlib", "libgda", "libva-intel-media-driver",
                        "obscure-thing", "libnma"]
            """))

        # Fake Utah overlay covering some names, listing others as
        # unavailable, and leaving some to be partitioned.
        (self.root / "utah.toml").write_text(textwrap.dedent("""\
            [parity]
            packages = ["wpa_supplicant", "wireless-regdb", "iw"]
            [unavailable]
            packages = ["libnma"]
            """))

        # Fake parity-ref.
        (self.root / ".bluefin-parity-ref").write_text("a" * 40)

        # Fake baseline file (created during --write test).
        self.baseline = self.root / "audit-baseline.json"

        self.saved = (audit.UTAH_TOML, audit.CONTAINERFILE, audit.BASELINE, audit.PARITY_REF, audit.HUMMINGBIRD_REPO_FILES)
        audit.UTAH_TOML = self.root / "utah.toml"
        audit.CONTAINERFILE = self.root / "Containerfile"
        audit.BASELINE = self.baseline
        audit.PARITY_REF = self.root / ".bluefin-parity-ref"
        audit.HUMMINGBIRD_REPO_FILES = (self.root / "hummingbird.repo",)
        (self.root / "hummingbird.repo").write_text(
            "[public-hummingbird-x86_64-rpms]\nname=hummingbird\n"
            "baseurl=https://example.invalid/hummingbird/\n"
            "enabled=1\n"
        )

    def tearDown(self):
        (audit.UTAH_TOML, audit.CONTAINERFILE, audit.BASELINE, audit.PARITY_REF, audit.HUMMINGBIRD_REPO_FILES) = self.saved

    def _mock_http(self):
        """Replace the network calls with file-copies of fake repodata.

        Patching `repository_metadata` and `fetch_hummingbird_repodata`
        directly is cleaner than mocking URL-by-URL: the audit's contract
        is the partition logic and the report, not the OCI transport. The
        smoke test asserts those.
        """
        from unittest.mock import patch
        from pathlib import Path

        def fake_factory_metadata(image: str, destination: Path) -> None:
            destination.mkdir(parents=True, exist_ok=True)
            # The script's `unpack_metadata` strips the `repository/` prefix
            # from tar paths, so files land at destination/repodata/<name>.
            # Replicate that layout here.
            for src in (self.factory_dir / "repository/repodata").iterdir():
                (destination / "repodata" / src.name).parent.mkdir(parents=True, exist_ok=True)
                (destination / "repodata" / src.name).write_bytes(src.read_bytes())

        def fake_hummingbird(destination: Path) -> tuple[str, str]:
            # The script writes the primary file under the repomd.xml-named
            # basename (repodata/primary.xml here). Mirror that.
            primary_basename = "primary.xml"
            (destination / primary_basename).write_bytes(
                (self.root / "hummingbird-primary.xml").read_bytes())
            return "https://example.invalid/hummingbird/", primary_basename

        def fake_bluefin(ref: str) -> str:
            return (self.root / "bluefin.toml").read_text()

        return [
            patch.object(audit, "repository_metadata", side_effect=fake_factory_metadata),
            patch.object(audit, "fetch_hummingbird_repodata", side_effect=fake_hummingbird),
            patch.object(audit, "bluefin_manifest", side_effect=fake_bluefin),
        ]

    def test_run_partitions_and_records_baseline(self):
        from unittest.mock import patch
        import argparse

        mocks = self._mock_http()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with contextlib.redirect_stdout(io.StringIO()):
                code = audit.cmd_run(argparse.Namespace(ref=None, write=True))
        self.assertEqual(code, 0)
        self.assertTrue(self.baseline.is_file())
        recorded = json.loads(self.baseline.read_text())
        self.assertIn("hummingbird-available", recorded)
        self.assertIn("factory-built", recorded)
        self.assertIn("nowhere", recorded)
        # libnma is in [unavailable] -> excluded from gap entirely.
        self.assertNotIn("libnma", recorded["hummingbird-available"])
        self.assertNotIn("libnma", recorded["factory-built"])
        self.assertNotIn("libnma", recorded["nowhere"])
        # wpa_supplicant, wireless-regdb, iw are in [parity] -> excluded.
        self.assertNotIn("wpa_supplicant", recorded["hummingbird-available"])
        # obscure-thing is in neither repo -> nowhere.
        self.assertIn("obscure-thing", recorded["nowhere"])
        # intel-gmmlib and libva-intel-media-driver are factory-built.
        self.assertIn("intel-gmmlib", recorded["factory-built"])
        self.assertIn("libva-intel-media-driver", recorded["factory-built"])

    def test_check_fails_when_a_new_name_appears_in_nowhere(self):
        from unittest.mock import patch
        import argparse

        # Write a baseline that does NOT mention "obscure-thing" -- a
        # new gap the audit must catch.
        self.baseline.write_text(json.dumps({
            "ref": "a" * 40,
            "factory_ref": "registry/pkgs@sha256:" + "f" * 64,
            "hummingbird_baseurl": "https://example.invalid/hummingbird/",
            "hummingbird-available": [],
            "factory-built": ["intel-gmmlib", "libva-intel-media-driver"],
            "nowhere": [],
        }))
        mocks = self._mock_http()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = audit.cmd_check(argparse.Namespace(ref=None))
        self.assertEqual(code, 1)
        self.assertIn("obscure-thing", err.getvalue())

    def test_check_silently_passes_when_partitions_match(self):
        from unittest.mock import patch
        import argparse

        # Run --write first to learn the current partition, then run --check
        # against that exact baseline. No growth, exit 0.
        mocks = self._mock_http()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(audit.cmd_run(argparse.Namespace(ref=None, write=True)), 0)
        mocks = self._mock_http()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = audit.cmd_check(argparse.Namespace(ref=None))
        self.assertEqual(code, 0, f"stderr was: {err.getvalue()}")

    def test_check_migrates_partition_silently(self):
        from unittest.mock import patch
        import argparse

        # A name moving from factory-built to hummingbird-available is a
        # Hummingbird rebuild landing. The gate must not flag it as drift.
        # First run --write to record the partition; then add the same
        # name to Hummingbird's primary.xml and re-run --check: the new
        # partition has the name in hummingbird-available (silent, because
        # that is exactly what the baseline says) and one fewer in
        # factory-built (also silent, because the gate ignores shrinks).
        mocks = self._mock_http()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(audit.cmd_run(argparse.Namespace(ref=None, write=True)), 0)
        baseline = json.loads(self.baseline.read_text())
        self.assertIn("intel-gmmlib", baseline["factory-built"])
        # Move intel-gmmlib into Hummingbird's primary.xml.
        hummingbird_with_intel = primary_xml({
            "wpa_supplicant": "2.11-1.fc44.x86_64",
            "wireless-regdb": "20240918-1.fc44.x86_64",
            "iw": "6.9-1.fc44.x86_64",
            "intel-gmmlib": "22.5.4-1.fc44.x86_64",
        })
        (self.root / "hummingbird-primary.xml").write_bytes(hummingbird_with_intel)
        # Update the baseline to reflect the migration.
        baseline["factory-built"].remove("intel-gmmlib")
        baseline["hummingbird-available"].append("intel-gmmlib")
        baseline["hummingbird-available"].sort()
        baseline["factory-built"].sort()
        self.baseline.write_text(json.dumps(baseline))
        mocks = self._mock_http()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = audit.cmd_check(argparse.Namespace(ref=None))
        self.assertEqual(code, 0, f"stderr was: {err.getvalue()}")


if __name__ == "__main__":
    unittest.main()
