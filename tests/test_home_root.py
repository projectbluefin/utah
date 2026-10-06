"""The image must ship an empty /var/home directory (#602).

Since /etc/default/useradd ships HOME=/home (#576) and /home is a symlink
to var/home, every `useradd --create-home` needs var/home to exist wherever
it runs: the installer chroot on a fresh install (no tmpfiles has run there
yet), the tacklebox customize container, and the live ISO build. All three
broke with `useradd: cannot create directory /home` (exit 12) when the
directory was missing.

The contract has two halves that must not drift apart:

- `system_files/.../tmpfiles.d/utah-home.conf` declares `d /var/home` so the
  directory is recreated at boot if missing and bootc lint's var-tmpfiles
  check stays green.
- The Containerfile creates /var/home after clean-stage (which strips all
  of /var except cache) but before `bootc container lint`, so lint still
  proves the tmpfiles coverage.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMPFILES = ROOT / "system_files/shared/usr/lib/tmpfiles.d/utah-home.conf"
CONTAINERFILE = ROOT / "Containerfile"


class HomeRootTests(unittest.TestCase):
    def test_tmpfiles_declares_home_root(self):
        """utah-home.conf must create /var/home as a root-owned directory."""
        self.assertTrue(TMPFILES.is_file(), "utah-home.conf is missing")
        lines = [
            line.split("#", 1)[0].strip()
            for line in TMPFILES.read_text().splitlines()
        ]
        self.assertIn(
            "d /var/home 0755 root root -",
            lines,
            "utah-home.conf must carry `d /var/home 0755 root root -`",
        )

    def test_containerfile_creates_home_after_clean_stage(self):
        """mkdir must run after clean-stage (which would delete it) ..."""
        text = CONTAINERFILE.read_text()
        clean = text.index("utah-clean-stage")
        mkdir = text.index("mkdir -p /var/home")
        lint = text.index("bootc container lint")
        self.assertLess(
            clean, mkdir, "/var/home must be created after clean-stage runs"
        )
        self.assertLess(
            mkdir,
            lint,
            "/var/home must exist before lint so lint proves tmpfiles coverage",
        )

    def test_mkdir_shares_the_lint_run(self):
        """The mkdir must stay inside the final RUN, not grow a new layer."""
        text = CONTAINERFILE.read_text()
        run_start = text.rindex("RUN ", 0, text.index("mkdir -p /var/home"))
        run_end = text.index("\n", text.index("bootc container lint"))
        run_block = text[run_start:run_end]
        self.assertIn("utah-clean-stage", run_block)
        self.assertIn("mkdir -p /var/home", run_block)
        self.assertIn("bootc container lint", run_block)


if __name__ == "__main__":
    unittest.main()
