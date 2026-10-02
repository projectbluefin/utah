"""`30-ghostty.sh` must point the Ghostty flatpak at ~/.config/ghostty, once.

The TunaOS `com.mitchellh.ghostty` build only sees
`~/.var/app/com.mitchellh.ghostty/config/ghostty/config`, so the hook moves any
config written there into `~/.config/ghostty` and symlinks the per-app path at
it. The body moves a user's only real config around, which makes both halves of
the once-only contract load-bearing: the gate must be the read-only
`version-script-check` (common#1196) so an aborted body retries next boot, and
the migration must not mistake a *reverse* symlink -- `~/.config/ghostty`
pointing into the per-app directory, the hand-rolled version of this same
workaround -- for a per-app config to migrate.

The first half of this file is a static contract check mirroring
`test_bootupctl_adopt_hook.py`; the second drives the real hook against a fake
libsetup and scratch homes for each state transition.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = (
    ROOT
    / "system_files/shared/usr/share/ublue-os/user-setup.hooks.d"
    / "30-ghostty.sh"
)
APP_ID = "com.mitchellh.ghostty"


class GhosttyHookContractTests(unittest.TestCase):
    def setUp(self):
        self.body = HOOK.read_text()
        self.lines = [
            line.strip()
            for line in self.body.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_hook_exists(self):
        self.assertTrue(HOOK.is_file())

    def test_does_not_use_the_legacy_burn_before_body_gate(self):
        self.assertNotIn(
            "version-script 30-ghostty user 1 || exit 0",
            self.lines,
            "the legacy helper stamps completion before the body runs, so a "
            "failed migration would never be retried",
        )

    def test_checks_before_it_acts(self):
        check = "version-script-check 30-ghostty user 1 || exit 0"
        self.assertIn(check, self.lines)
        self.assertLess(
            self.lines.index(check),
            self.lines.index('ln -sfn "${host_dir}" "${sandbox_dir}"'),
        )

    def test_commits_after_the_body(self):
        commit = "version-script-commit 30-ghostty user 1"
        self.assertIn(commit, self.lines)
        self.assertLess(
            self.lines.index('ln -sfn "${host_dir}" "${sandbox_dir}"'),
            self.lines.index(commit),
        )

    def test_body_failure_aborts_before_the_commit(self):
        self.assertIn("set -euo pipefail", self.lines)
        self.assertLess(
            self.lines.index("set -euo pipefail"),
            self.lines.index('ln -sfn "${host_dir}" "${sandbox_dir}"'),
        )

    def test_compat_shim_covers_pre_1196_libsetup(self):
        self.assertIn(
            "if ! declare -F version-script-check >/dev/null; then", self.body
        )
        self.assertIn("version-script-commit() { :; }", self.body)

    def test_sources_libsetup_via_the_conventional_path(self):
        self.assertIn(
            "source /usr/lib/ublue/setup-services/libsetup.sh", self.body
        )

    def test_seeded_config_hint_names_a_command_that_exists(self):
        """Ghostty ships only as the flatpak (iso/live/src/install-flatpaks.sh);
        there is no `ghostty` binary on the host to run."""
        self.assertIn(
            "flatpak run com.mitchellh.ghostty +show-config --default --docs",
            self.body,
        )


class GhosttyHookBehaviourTests(unittest.TestCase):
    """Drive the real hook against scratch homes for each state transition."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = Path(self.tmp.name)
        self.home = self.fake / "home"
        self.home.mkdir()

        libsetup_dir = self.fake / "libexec"
        libsetup_dir.mkdir()
        (libsetup_dir / "libsetup.sh").write_text(
            "#!/usr/bin/env bash\n"
            'version-script() {\n'
            '  local key="$1" scope="$2" version="$3"\n'
            '  local marker="${HOOK_STATE_DIR}/${scope}-${key}-${version}"\n'
            '  [[ -e "${marker}" ]] && return 1\n'
            '  return 0\n'
            '}\n'
            'version-script-check() {\n'
            '  local key="$1" scope="$2" version="$3"\n'
            '  local marker="${HOOK_STATE_DIR}/${scope}-${key}-${version}"\n'
            '  [[ -e "${marker}" ]] && return 1\n'
            '  return 0\n'
            '}\n'
            'version-script-commit() {\n'
            '  local key="$1" scope="$2" version="$3"\n'
            '  local marker="${HOOK_STATE_DIR}/${scope}-${key}-${version}"\n'
            '  mkdir -p "${HOOK_STATE_DIR}"\n'
            '  : > "${marker}"\n'
            '}\n'
        )

        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)
        self.env.pop("XDG_CONFIG_HOME", None)
        self.env["HOOK_STATE_DIR"] = str(self.fake / "state")

    # -- helpers ---------------------------------------------------------
    @property
    def host_dir(self):
        return self.home / ".config" / "ghostty"

    @property
    def sandbox_dir(self):
        return self.home / ".var" / "app" / APP_ID / "config" / "ghostty"

    @property
    def marker(self):
        return self.fake / "state" / "user-30-ghostty-1"

    def run_hook(self):
        """Run the real hook with libsetup redirected at the stub."""
        patched = HOOK.read_text().replace(
            "/usr/lib/ublue/setup-services/libsetup.sh",
            str(self.fake / "libexec" / "libsetup.sh"),
        )
        wrapper = self.fake / "hook.sh"
        wrapper.write_text(patched)
        wrapper.chmod(0o755)
        return subprocess.run(
            ["bash", str(wrapper)], capture_output=True, text=True, env=self.env
        )

    def assert_linked(self):
        self.assertTrue(
            self.sandbox_dir.is_symlink(),
            "the per-app config dir must be a symlink at the host config",
        )
        self.assertEqual(
            self.sandbox_dir.resolve(), self.host_dir.resolve()
        )
        self.assertFalse(
            self.host_dir.is_symlink(),
            "the host config dir must hold the real files",
        )

    def migrated_dirs(self):
        return sorted(self.sandbox_dir.parent.glob("ghostty.utah-migrated-*"))

    # -- transitions -----------------------------------------------------
    def test_fresh_user_gets_the_light_dark_default_and_the_symlink(self):
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_linked()
        config = (self.host_dir / "config").read_text()
        self.assertIn("theme = light:Adwaita,dark:Adwaita Dark", config)
        self.assertTrue(self.marker.exists())

    def test_existing_per_app_config_is_migrated_not_lost(self):
        self.sandbox_dir.mkdir(parents=True)
        (self.sandbox_dir / "config").write_text("font-size = 13\n")
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_linked()
        self.assertEqual(
            (self.host_dir / "config").read_text(), "font-size = 13\n"
        )
        migrated = self.migrated_dirs()
        self.assertEqual(len(migrated), 1, migrated)
        self.assertEqual((migrated[0] / "config").read_text(), "font-size = 13\n")

    def test_existing_host_config_is_never_overwritten(self):
        self.host_dir.mkdir(parents=True)
        (self.host_dir / "config").write_text("font-size = 20\n")
        self.sandbox_dir.mkdir(parents=True)
        (self.sandbox_dir / "config").write_text("font-size = 13\n")
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_linked()
        self.assertEqual(
            (self.host_dir / "config").read_text(), "font-size = 20\n"
        )

    def test_reverse_symlink_keeps_the_config_and_fixes_the_direction(self):
        """`~/.config/ghostty` symlinked into the per-app dir is the plausible
        hand-rolled workaround for this same bug. Migrating it as if it were a
        plain per-app config copied the directory onto itself, moved the only
        real config aside, and left the host path dangling, so the seed write
        failed and the user ended with no config at either path."""
        self.sandbox_dir.mkdir(parents=True)
        (self.sandbox_dir / "config").write_text("font-size = 11\n")
        self.host_dir.parent.mkdir(parents=True, exist_ok=True)
        self.host_dir.symlink_to(self.sandbox_dir)

        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_linked()
        self.assertEqual(
            (self.host_dir / "config").read_text(), "font-size = 11\n"
        )
        self.assertEqual(self.migrated_dirs(), [])
        self.assertTrue(self.marker.exists())

    def test_dangling_reverse_symlink_is_replaced_by_a_fresh_config(self):
        self.host_dir.parent.mkdir(parents=True, exist_ok=True)
        self.host_dir.symlink_to(self.sandbox_dir)
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_linked()
        self.assertIn(
            "theme = light:Adwaita,dark:Adwaita Dark",
            (self.host_dir / "config").read_text(),
        )

    def test_host_symlink_elsewhere_is_left_alone(self):
        """A dotfiles-managed ~/.config/ghostty points somewhere unrelated; the
        hook must migrate through it, not delete it."""
        dotfiles = self.home / "dotfiles" / "ghostty"
        dotfiles.mkdir(parents=True)
        self.host_dir.parent.mkdir(parents=True, exist_ok=True)
        self.host_dir.symlink_to(dotfiles)
        self.sandbox_dir.mkdir(parents=True)
        (self.sandbox_dir / "config").write_text("font-size = 9\n")

        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.host_dir.is_symlink())
        self.assertEqual(self.host_dir.resolve(), dotfiles.resolve())
        self.assertEqual((dotfiles / "config").read_text(), "font-size = 9\n")
        self.assertEqual(self.sandbox_dir.resolve(), dotfiles.resolve())

    def test_sandbox_symlink_to_dotfiles_is_left_alone(self):
        """The per-app path already symlinked at a user's dotfiles directory is
        this same fix by hand. Replacing it with a link to an empty, freshly
        seeded ~/.config/ghostty would silently ignore their config -- the very
        bug this hook exists to fix."""
        dotfiles = self.home / "dotfiles" / "ghostty"
        dotfiles.mkdir(parents=True)
        (dotfiles / "config").write_text("font-size = 17\n")
        self.sandbox_dir.parent.mkdir(parents=True)
        self.sandbox_dir.symlink_to(dotfiles)

        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.sandbox_dir.is_symlink())
        self.assertEqual(self.sandbox_dir.resolve(), dotfiles.resolve())
        self.assertEqual((dotfiles / "config").read_text(), "font-size = 17\n")
        self.assertFalse(
            self.host_dir.exists(),
            "the hook must not conjure an empty host config directory",
        )
        self.assertFalse(
            self.marker.exists(),
            "leaving without doing the work must not burn the stamp, so a "
            "later boot can still set this up if the user drops their link",
        )

    def test_both_paths_symlinked_at_one_dotfiles_dir_keeps_the_config(self):
        """Both ~/.config/ghostty and the per-app path pointing at the same
        third directory makes the two `readlink -f` targets equal, which used
        to trip the reverse-symlink repair: it deleted the host link while the
        `mv` declined to put anything back, losing both links to the config."""
        dotfiles = self.home / "dotfiles" / "ghostty"
        dotfiles.mkdir(parents=True)
        (dotfiles / "config").write_text("font-size = 19\n")
        self.host_dir.parent.mkdir(parents=True, exist_ok=True)
        self.host_dir.symlink_to(dotfiles)
        self.sandbox_dir.parent.mkdir(parents=True)
        self.sandbox_dir.symlink_to(dotfiles)

        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(
            self.host_dir.is_symlink(), "the user's host link must survive"
        )
        self.assertEqual(self.host_dir.resolve(), dotfiles.resolve())
        self.assertEqual(self.sandbox_dir.resolve(), dotfiles.resolve())
        self.assertEqual((dotfiles / "config").read_text(), "font-size = 19\n")
        self.assertEqual(self.migrated_dirs(), [])

    def test_already_symlinked_run_is_idempotent(self):
        first = self.run_hook()
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.host_dir / "config").write_text("font-size = 14\n")
        # Clear the marker so the body runs again, as it would under the legacy
        # shim on a rebuilt stamp; the result must be unchanged.
        self.marker.unlink()
        second = self.run_hook()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assert_linked()
        self.assertEqual(
            (self.host_dir / "config").read_text(), "font-size = 14\n"
        )
        self.assertEqual(self.migrated_dirs(), [])

    def test_second_boot_skips_the_body(self):
        first = self.run_hook()
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.host_dir / "config").write_text("font-size = 14\n")
        self.sandbox_dir.unlink()
        second = self.run_hook()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(
            self.sandbox_dir.exists(),
            "a committed hook must not touch the per-app dir again",
        )

    def test_xdg_config_home_is_honoured(self):
        xdg = self.home / "xdg"
        xdg.mkdir()
        self.env["XDG_CONFIG_HOME"] = str(xdg)
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((xdg / "ghostty" / "config").is_file())
        self.assertEqual(
            self.sandbox_dir.resolve(), (xdg / "ghostty").resolve()
        )


if __name__ == "__main__":
    unittest.main()
