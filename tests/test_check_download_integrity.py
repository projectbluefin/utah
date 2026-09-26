"""Tests for scripts/check-download-integrity.py.

The download integrity script ensures that no build recipe fetches unpinned,
unverified executables or resolves mutable `releases/latest` URLs.
These tests verify that the gate reliably rejects mutable and unverified
downloads, sees downloads whose URL sits on a backslash continuation line,
honors verifier tokens only inside the download's own scope,
respects comments and allowlists, and passes on the shipped repository.
"""

from __future__ import annotations

import importlib.util
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

# Clearance tokens, matched on word boundaries by the gate. A bare `--check`
# is not one: it substring-matched flags like `--checkpoint`, while the real
# forms `sha256sum --check` / `sha512sum --check` are covered by the digest
# tokens themselves.
VERIFIER_TOKENS = [
    "sha256sum",
    "sha512sum",
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

    def test_digest_before_the_fetch_does_not_clear_it(self):
        # One token anywhere in the file used to clear every download in
        # that file. Clearance runs from the fetch onward, not backward.
        self.write_file(
            "Containerfile",
            "RUN echo 'expected' | sha256sum --check --strict\n"
            "RUN curl -LO https://example.com/sneaky.rpm\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "Containerfile:2: executable download without a digest or signature check",
            result.stderr,
        )

    def test_digest_beyond_the_scope_window_does_not_clear(self):
        lines = ["RUN curl -LO https://example.com/sneaky.rpm"]
        lines += [f"# padding {number}" for number in range(1, 12)]
        lines += ["RUN echo 'expected' | sha256sum --check --strict"]
        self.write_file("Containerfile", "\n".join(lines) + "\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "Containerfile:1: executable download without a digest or signature check",
            result.stderr,
        )

    def test_verifier_inside_a_long_continuation_run_clears(self):
        # The digest check sits past the line window but in the same
        # backslash-joined command as the fetch, so it still vouches.
        run = ["RUN curl -LO https://example.com/payload.rpm \\"]
        run += [f"    -H 'X-pad{number}: 1' \\" for number in range(1, 12)]
        run += ["    && echo 'expected' | sha256sum --check --strict"]
        self.write_file("Containerfile", "\n".join(run) + "\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_verifier_inside_a_long_and_chain_clears(self):
        # Same for an `&&` chain written without backslashes: every line but
        # the last ends in `&&`, and the chain is the fetch's scope.
        chain = ["RUN curl -LO https://example.com/payload.rpm &&"]
        chain += [f"    echo step{number} &&" for number in range(1, 12)]
        chain += ["    echo 'expected' | sha256sum --check --strict"]
        self.write_file("Containerfile", "\n".join(chain) + "\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_comment_claiming_verification_does_not_clear(self):
        self.write_file(
            "Containerfile",
            "RUN curl -LO https://example.com/payload.rpm\n"
            "# verified with sha256sum before extraction\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "Containerfile:1: executable download without a digest or signature check",
            result.stderr,
        )

    def test_checklike_flags_do_not_clear(self):
        # `--check` used to match as a bare substring anywhere in the file.
        for flag_line in ("RUN tar --checkpoint=1 -xf /tmp/src.tar", "RUN tool --check config"):
            with self.subTest(flag_line=flag_line):
                with tempfile.TemporaryDirectory() as sub_tmp:
                    sub_dir = Path(sub_tmp)
                    target = sub_dir / "Containerfile"
                    target.write_text(
                        "RUN curl -LO https://example.com/payload.rpm\n" + flag_line + "\n"
                    )
                    result = subprocess.run(
                        [sys.executable, str(SCRIPT)],
                        cwd=sub_dir,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(
                        "Containerfile:1: executable download without a digest or signature check",
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

    def test_download_split_across_continuation_lines_is_flagged(self):
        self.write_file(
            "scripts/configure-services.sh",
            "curl -fsSL \\\n"
            "    --output /tmp/payload.rpm \\\n"
            "    https://example.com/payload.rpm\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "scripts/configure-services.sh:1: executable download without a digest or signature check",
            result.stderr,
        )

    def test_mutable_latest_release_split_across_continuation_lines_is_flagged(self):
        self.write_file(
            "Containerfile",
            "RUN echo building \\\n"
            " && curl -LO \\\n"
            "      https://github.com/org/repo/releases/latest/download/tool.tar.gz\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Containerfile:1: resolves a mutable latest release", result.stderr)

    def test_continuation_download_reports_the_line_the_command_starts_on(self):
        self.write_file(
            "scripts/configure-services.sh",
            "echo one\n"
            "echo two\n"
            "curl -fsSL \\\n"
            "    https://example.com/driver.run\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "scripts/configure-services.sh:3: executable download without a digest or signature check",
            result.stderr,
        )

    def test_allowlisted_descriptor_split_across_continuation_lines_is_exempt(self):
        self.write_file(
            "scripts/configure-services.sh",
            "curl --fail --silent \\\n"
            "    --output /etc/flatpak/remotes.d/flathub.flatpakrepo \\\n"
            "    https://dl.flathub.org/repo/flathub.flatpakrepo\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_commented_continuation_block_stays_exempt(self):
        self.write_file(
            "Containerfile",
            "# curl -LO \\\n"
            "#     https://example.com/payload.rpm\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_comment_ending_in_backslash_does_not_swallow_the_next_download(self):
        # A trailing backslash in a shell comment ends at the newline, so the
        # curl below is a separate, unverified command and must be flagged.
        self.write_file(
            "Containerfile",
            "# fetch the installer bundle \\\n"
            "curl -fsSL https://example.com/payload.tar.gz -o /tmp/p.tar.gz\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "Containerfile:2: executable download without a digest or signature check",
            result.stderr,
        )

    def test_comment_inside_a_continuation_run_does_not_hide_the_download(self):
        # The Dockerfile parser drops comment lines inside a RUN continuation,
        # so the URL still belongs to the curl above it.
        self.write_file(
            "Containerfile",
            "RUN curl -fsSL \\\n"
            "    # the installer payload\n"
            "    https://example.com/payload.tar.gz -o /tmp/p.tar.gz\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "Containerfile:1: executable download without a digest or signature check",
            result.stderr,
        )

    def test_verifier_on_the_same_continued_command_clears_it(self):
        self.write_file(
            "Containerfile",
            "RUN curl -fsSL -o /tmp/tool.tar.gz https://example.com/tool.tar.gz \\\n"
            " && echo \"${SHA}  /tmp/tool.tar.gz\" | sha256sum --check --strict\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)


class LogicalLineTests(unittest.TestCase):
    """Directly exercise the continuation joining used by the gate."""

    @staticmethod
    def load_logical_lines():
        spec = importlib.util.spec_from_file_location("check_download_integrity", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.logical_lines

    def test_plain_lines_keep_their_numbers(self):
        logical_lines = self.load_logical_lines()
        self.assertEqual(
            logical_lines("alpha\nbeta\ngamma\n"),
            [(1, "alpha"), (2, "beta"), (3, "gamma")],
        )

    def test_continuations_collapse_to_the_starting_line(self):
        logical_lines = self.load_logical_lines()
        self.assertEqual(
            logical_lines("one \\\n  two \\\n  three\nfour\n"),
            [(1, "one two three"), (4, "four")],
        )

    def test_comment_with_trailing_backslash_never_continues(self):
        logical_lines = self.load_logical_lines()
        self.assertEqual(
            logical_lines("# note \\\ncurl -fsSL https://example.com/p.tar.gz\n"),
            [(1, "# note \\"), (2, "curl -fsSL https://example.com/p.tar.gz")],
        )

    def test_comment_inside_a_run_is_dropped_and_the_run_continues(self):
        logical_lines = self.load_logical_lines()
        self.assertEqual(
            logical_lines("RUN curl -fsSL \\\n  # payload\n  https://example.com/p.tar.gz\n"),
            [(1, "RUN curl -fsSL https://example.com/p.tar.gz")],
        )

    def test_blank_line_before_a_command_does_not_own_its_number(self):
        logical_lines = self.load_logical_lines()
        self.assertEqual(
            logical_lines("\ncurl -fsSL \\\n  https://example.com/p.tar.gz\n"),
            [(1, ""), (2, "curl -fsSL https://example.com/p.tar.gz")],
        )

    def test_trailing_continuation_without_a_successor_is_still_emitted(self):
        logical_lines = self.load_logical_lines()
        self.assertEqual(logical_lines("dangling \\\n"), [(1, "dangling")])


if __name__ == "__main__":
    unittest.main()
