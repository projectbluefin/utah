"""Firefox setup removes retired Bluefin preferences without deleting user files."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "system_files/shared/usr/share/ublue-os/privileged-setup.hooks.d/99-flatpaks.sh"


class FlatpaksHookGlobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="flatpak-prefs-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "path with spaces"
        self.root.mkdir()
        self.source = self.root / "firefox-config"
        self.source.mkdir()
        self.flatpak_root = self.root / "flatpak"
        self.destination = self.flatpak_root / "extension/org.mozilla.firefox.systemconfig/x86_64/stable/defaults/pref"
        self.destination.mkdir(parents=True)
        self.libsetup = self.root / "libsetup.sh"
        self.libsetup.write_text("version-script() { return 0; }\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        arch = self.bin / "arch"
        arch.write_text("#!/bin/sh\nprintf 'x86_64\\n'\n")
        arch.chmod(0o755)

    def run_hook(self):
        body = HOOK.read_text().replace(
            "source /usr/lib/ublue/setup-services/libsetup.sh",
            f'source "{self.libsetup}"',
        ).replace("/var/lib/flatpak", str(self.flatpak_root)).replace(
            "/usr/share/ublue-os/firefox-config", str(self.source)
        )
        script = self.root / "hook.sh"
        script.write_text(body)
        return subprocess.run(
            ["bash", str(script)], text=True, capture_output=True,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"},
        )

    def test_retired_preferences_removed_but_user_preferences_preserved(self):
        stale = self.destination / "old-bluefin-default.js"
        stale.write_text("retired preference\n")
        user = self.destination / "user-custom.js"
        user.write_text("user preference\n")
        (self.source / "new-bluefin-default.js").write_text("new preference\n")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())
        self.assertEqual(user.read_text(), "user preference\n")
        self.assertEqual((self.destination / "new-bluefin-default.js").read_text(), "new preference\n")

    def test_no_stale_match_still_installs_the_new_preferences(self):
        (self.source / "current-bluefin.js").write_text("current preference\n")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.destination / "current-bluefin.js").read_text(), "current preference\n")
