"""Tests for scripts/check-download-integrity.py.

The download integrity script ensures that no build recipe fetches unpinned,
unverified executables or resolves mutable `releases/latest` URLs.
These tests verify that the gate reliably rejects mutable and unverified
downloads, honors verifier tokens per-file, respects comments and allowlists,
and passes on the shipped repository.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-download-integrity.py"

INSPECTED_FILES = [
    "Containerfile",
    "Containerfile.kernel",
    "scripts/install-nvidia.sh",
    "scripts/install-ogc-kernel.sh",
    "scripts/configure-services.sh",
    "iso/live/src/install-flatpaks.sh",
    "iso/scripts/build-iso.sh",
]

GUARDED_EXTENSIONS = [
    "run",
    "tar.gz",
    "tgz",
    "rpm",
    "flatpak",
    "service",
    "timer",
]

VERIFIER_TOKENS = [
    "sha256sum",
    "sha512sum",
    "--check",
    "cosign",
    "gpg --verify",
]


class DownloadIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_check(self, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=cwd or self.dir,
            capture_output=True,
            text=True,
        )

    def write_file(self, rel_path: str, content: str) -> Path:
        target = self.dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def test_shipped_tree_passes(self):
        result = self.run_check(cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("checked 7 build recipes: downloads are pinned and verified", result.stdout)

    def test_all_seven_inspected_paths_reject_mutable_latest_release(self):
        for path_str in INSPECTED_FILES:
            with self.subTest(path=path_str):
                with tempfile.TemporaryDirectory() as sub_tmp:
                    sub_dir = Path(sub_tmp)
                    target = sub_dir / path_str
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("curl -LO https://github.com/org/repo/releases/latest/download/tool.tar.gz\n")
                    result = subprocess.run(
                        [sys.executable, str(SCRIPT)],
                        cwd=sub_dir,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("Unpinned or unverified executable downloads:", result.stderr)
                    self.assertIn(f"{path_str}:1: resolves a mutable latest release", result.stderr)

    def test_uninspected_file_path_is_ignored(self):
        self.write_file(
            "scripts/uninspected.sh",
            "curl -LO https://github.com/org/repo/releases/latest/download/tool.tar.gz\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_guarded_suffix_and_both_curl_and_wget(self):
        for tool in ("curl", "wget"):
            for ext in GUARDED_EXTENSIONS:
                with self.subTest(tool=tool, ext=ext):
                    with tempfile.TemporaryDirectory() as sub_tmp:
                        sub_dir = Path(sub_tmp)
                        target = sub_dir / "Containerfile"
                        target.write_text(f"RUN {tool} -LO https://example.com/downloads/package.{ext}\n")
                        result = subprocess.run(
                            [sys.executable, str(SCRIPT)],
                            cwd=sub_dir,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(result.returncode, 1)
                        self.assertIn("Unpinned or unverified executable downloads:", result.stderr)
                        self.assertIn(
                            "Containerfile:1: executable download without a digest or signature check",
                            result.stderr,
                        )

    def test_unguarded_suffixes_are_not_flagged(self):
        self.write_file(
            "Containerfile",
            "RUN curl -LO https://example.com/asset.txt\n"
            "RUN wget https://example.com/config.json\n"
            "RUN curl -LO https://example.com/logo.png\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_verifier_token_clears_download(self):
        for token in VERIFIER_TOKENS:
            with self.subTest(token=token):
                with tempfile.TemporaryDirectory() as sub_tmp:
                    sub_dir = Path(sub_tmp)
                    target = sub_dir / "Containerfile"
                    target.write_text(
                        "RUN curl -LO https://example.com/asset.tar.gz\n"
                        f"RUN echo 'hash' | {token}\n"
                    )
                    result = subprocess.run(
                        [sys.executable, str(SCRIPT)],
                        cwd=sub_dir,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_verifier_clearance_is_per_file_not_repo_wide(self):
        self.write_file(
            "Containerfile",
            "RUN curl -LO https://example.com/asset.tar.gz\n"
            "RUN echo 'expected' | sha256sum --check\n",
        )
        self.write_file(
            "scripts/install-nvidia.sh",
            "curl -LO https://example.com/driver.run\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "scripts/install-nvidia.sh:1: executable download without a digest or signature check",
            result.stderr,
        )
        self.assertNotIn("Containerfile", result.stderr)

    def test_comment_lines_are_exempt(self):
        self.write_file(
            "Containerfile",
            "# curl -LO https://github.com/org/repo/releases/latest/download/tool.tar.gz\n"
            "  # wget https://example.com/package.rpm\n"
            "# curl -LO https://example.com/driver.run\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_flathub_allowlist_is_exempt(self):
        self.write_file(
            "iso/live/src/install-flatpaks.sh",
            "curl -LO https://dl.flathub.org/repo/flathub.flatpakrepo\n"
            "wget https://dl.flathub.org/repo/appstream\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_line_number_reported_accurately(self):
        self.write_file(
            "scripts/configure-services.sh",
            "# line 1\n"
            "# line 2\n"
            "echo 'configuring'\n"
            "curl -LO https://example.com/daemon.service\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "scripts/configure-services.sh:4: executable download without a digest or signature check",
            result.stderr,
        )

    def test_mutable_latest_release_rejected_even_with_verifier_present(self):
        self.write_file(
            "Containerfile",
            "curl -LO https://github.com/org/repo/releases/latest/download/tool.tar.gz\n"
            "echo 'hash' | sha256sum --check\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Containerfile:1: resolves a mutable latest release", result.stderr)


if __name__ == "__main__":
    unittest.main()
