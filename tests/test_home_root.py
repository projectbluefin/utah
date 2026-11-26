"""/home is Dakota's bind mount of /var/home over an empty image directory.

Since /etc/default/useradd ships HOME=/home (#576) and home.mount binds
/var/home over /home (#1234, Dakota's files/bindmounts/home.mount), the homes
live in /var/home, which must exist wherever homes are created: the
installer's stateroot and the install medium's live session (#602).

The contract has three parts that must not drift apart:

- `system_files/.../tmpfiles.d/utah-home.conf` declares `d /var/home` so the
  directory is recreated at boot if missing and bootc lint's var-tmpfiles
  check stays green.
- The Containerfile creates /var/home after clean-stage (which strips all
  of /var except cache) but before `bootc container lint`, so lint still
  proves the tmpfiles coverage, and replaces Hummingbird's /home -> var/home
  symlink with an empty directory in the same RUN.
- home.mount is wanted statically by local-fs.target, and
  bluefin-migrate-var-home-passwd rewrites legacy /var/home passwd fields.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "system_files/shared"
TMPFILES = SHARED / "usr/lib/tmpfiles.d/utah-home.conf"
UNITS = SHARED / "usr/lib/systemd/system"
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
        # /home becomes the empty bind-mount point in the same layer.
        self.assertIn("if [ -L /home ]; then rm /home; fi", run_block)
        self.assertIn("mkdir -p -m 0755 /home", run_block)
        self.assertIn('if [ -n "$(ls -A /home)" ]', run_block)
        self.assertLess(run_block.index("mkdir -p -m 0755 /home"),
                        run_block.index("bootc container lint"))
        self.assertIn(
            "mkdir -p /var/home /efi",
            run_block,
            "/efi must exist in the image for systemd's EFI automount (#1326)",
        )

    def test_home_mount_binds_var_home_from_local_fs(self):
        mount = (UNITS / "home.mount").read_text()
        for line in ("What=/var/home", "Where=/home", "Type=none", "Options=bind",
                     "ConditionPathIsDirectory=/var/home"):
            self.assertIn(line, mount.splitlines())
        link = UNITS / "local-fs.target.wants/home.mount"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../home.mount")

    def test_passwd_migration_runs_before_logins(self):
        script = SHARED / "usr/libexec/bluefin-migrate-var-home-passwd"
        self.assertTrue(os.access(script, os.X_OK))
        unit = (UNITS / "bluefin-migrate-var-home-passwd.service").read_text()
        self.assertIn("ExecStart=/usr/libexec/bluefin-migrate-var-home-passwd", unit)
        for before in ("systemd-user-sessions.service", "accounts-daemon.service",
                       "gdm.service"):
            self.assertIn(f"Before={before}", unit)
        preset = SHARED / "usr/lib/systemd/system-preset/80-bluefin-migrate-var-home-passwd.preset"
        self.assertEqual(preset.read_text(), "enable bluefin-migrate-var-home-passwd.service\n")

    def run_migration(self, passwd: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "root"
            (prefix / "etc").mkdir(parents=True)
            (prefix / "etc/passwd").write_text(passwd)
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            calls = Path(tmp) / "calls"
            stub = bin_dir / "usermod"
            stub.write_text(f'#!/bin/sh\necho "$*" >>"{calls}"\n')
            stub.chmod(0o755)
            result = subprocess.run(
                ["bash", str(SHARED / "usr/libexec/bluefin-migrate-var-home-passwd")],
                capture_output=True, text=True,
                env={**os.environ, "MIGRATE_PREFIX": str(prefix),
                     "PATH": f"{bin_dir}:{os.environ['PATH']}"})
            return result, calls.read_text().splitlines() if calls.exists() else []

    def test_passwd_migration_rewrites_var_home_users(self):
        # Dakota's fast-path regex only matched a bare "/var/home/" field, so
        # /var/home/<user> entries were never migrated; Utah's copy fixes it.
        result, calls = self.run_migration(
            "root:x:0:0::/var/roothome:/bin/bash\n"
            "alice:x:1000:1000::/var/home/alice:/bin/bash\n"
            "bob:x:1001:1001::/home/bob:/bin/bash\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c.split(" ", 2)[2] for c in calls],
                         ["-d /root root", "-d /home/alice alice"])
        self.assertTrue(all(c.startswith("--prefix ") for c in calls), calls)

    def test_passwd_migration_is_a_no_op_without_legacy_fields(self):
        result, calls = self.run_migration("bob:x:1001:1001::/home/bob:/bin/bash\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

if __name__ == "__main__":
    unittest.main()
