"""`05-bootupctl-adopt.sh` must adopt the on-disk bootloader, on first boot only.

A system switched into Utah (or fresh-installed from any image that did not run
bootupd's adopt step) leaves /boot/bootupd-state.json unwritten: `bootupctl
status` then reports "No components installed" and no shim or GRUB updates are
ever offered, even though every payload is on disk. The hook runs
`bootupctl adopt-and-update` on first boot to fix that, using the common#1196
`version-script-check`/`version-script-commit` pair so a failed first boot
retries next boot instead of being skipped forever after.

The first half of this file is a static contract check that mirrors
`test_setup_hook_version_contract.py`. The second half drives the real hook
against fake libsetup/bootupctl stubs to verify the four state transitions
on the host, without an image or bootupctl installed.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = (
    ROOT
    / "system_files/shared/usr/share/ublue-os/privileged-setup.hooks.d"
    / "05-bootupctl-adopt.sh"
)


class BootupctlAdoptHookContractTests(unittest.TestCase):
    def setUp(self):
        self.lines = [
            line.strip()
            for line in HOOK.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.body = HOOK.read_text()

    def test_hook_exists(self):
        self.assertTrue(HOOK.is_file())

    def test_runs_before_other_privileged_hooks(self):
        """Lexicographic order is how ublue-privileged-setup dispatches hooks;
        a hook that adopts the bootloader must run before any later hook that
        might inspect bootupctl state."""
        for later in ("10-tailscale.sh", "11-framework-ucsi-workaround.sh",
                      "20-home-labels.sh", "99-flatpaks.sh"):
            self.assertLess("05-bootupctl-adopt.sh", later)

    def test_does_not_use_the_legacy_burn_before_body_gate(self):
        self.assertNotIn(
            "version-script bootupctl-adopt privileged 1",
            self.lines,
            "the legacy helper stamps completion before the body runs",
        )

    def test_checks_before_it_acts(self):
        check = "version-script-check bootupctl-adopt privileged 1 || exit 0"
        self.assertIn(check, self.lines)
        self.assertLess(
            self.lines.index(check),
            self.lines.index("bootupctl adopt-and-update"),
        )

    def test_commits_after_the_body(self):
        self.assertIn(
            "version-script-commit bootupctl-adopt privileged 1", self.lines
        )
        self.assertLess(
            self.lines.index("bootupctl adopt-and-update"),
            self.lines.index("version-script-commit bootupctl-adopt privileged 1"),
        )

    def test_body_failure_aborts_before_the_commit(self):
        # `set -e` is what makes a failing adopt-and-update stop the hook
        # instead of running on to record a completion that never happened.
        self.assertIn("set -xeuo pipefail", self.lines)
        self.assertLess(
            self.lines.index("set -xeuo pipefail"),
            self.lines.index("bootupctl adopt-and-update"),
        )

    def test_uses_adopt_and_update_not_update_alone(self):
        # `bootupctl update` is the steady-state updater; the first call must
        # be `adopt-and-update`, otherwise the on-disk state is never recorded
        # and the hook does nothing useful on its target systems.
        self.assertIn("bootupctl adopt-and-update", self.body)
        self.assertNotIn("bootupctl update\n", self.body)
        self.assertNotIn("bootupctl update ;", self.body)
        self.assertNotIn("bootupctl update &&", self.body)
        self.assertNotIn("bootupctl update||", self.body)

    def test_skips_live_sessions(self):
        """A live ISO root (erofs or squashfs) cannot have a managed ESP; the
        hook must recognise it and skip without committing, so a booted live
        image retries until it has been installed."""
        self.assertIn("/sysroot", self.body)
        self.assertIn("erofs", self.body)
        self.assertIn("squashfs", self.body)

    def test_skips_when_bootupctl_missing(self):
        """An image variant that does not carry bootupctl must not commit and
        cause the hook to be skipped forever after common#1196."""
        self.assertIn("command -v bootupctl", self.body)

    def test_compat_shim_covers_pre_1196_libsetup(self):
        # The pinned COMMON_IMAGE_SHA has no version-script-check/-commit yet,
        # so the shim keeps the hook working under both contracts in either
        # merge order.
        self.assertIn("if ! declare -F version-script-check >/dev/null; then", self.body)
        self.assertIn("version-script-commit() { :; }", self.body)

    def test_sources_libsetup_via_the_conventional_path(self):
        # The other privileged-setup hooks source libsetup.sh from
        # /usr/lib/ublue/setup-services/libsetup.sh. A new hook must do the
        # same or dispatch will source nothing.
        self.assertIn(
            "source /usr/lib/ublue/setup-services/libsetup.sh", self.body
        )


class BootupctlAdoptHookBehaviourTests(unittest.TestCase):
    """Drive the real hook against stub binaries that emulate the four first-boot
    transitions: a fresh install, a re-run after success, a live session, and a
    system without bootupctl installed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = Path(self.tmp.name)

        # libsetup.sh stub implementing both legacy version-script and the new
        # version-script-check/-commit pair from common#1196.
        libsetup_dir = self.fake / "libexec"
        libsetup_dir.mkdir()
        libsetup = libsetup_dir / "libsetup.sh"
        libsetup.write_text(
            "#!/usr/bin/env bash\n"
            # Legacy version-script: return 0 means "do the work", return 1
            # means "already done".
            'version-script() {\n'
            '  local key="$1" scope="$2" version="$3"\n'
            '  local marker="${HOOK_STATE_DIR}/${scope}-${key}-${version}"\n'
            '  [[ -e "${marker}" ]] && return 1\n'
            '  return 0\n'
            '}\n'
            # Common#1196 pair: check is read-only, commit is the record.
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

        # findmnt stub. Default lives on a normal ext4 root; tests override.
        bin_dir = self.fake / "bin"
        bin_dir.mkdir()
        findmnt = bin_dir / "findmnt"
        findmnt.write_text("#!/usr/bin/env bash\necho \"${HOOK_ROOT_FSTYPE}\"\n")
        findmnt.chmod(0o755)

        # bootupctl stub. The flag file `bootupctl_args` records the args it
        # was called with, so tests can assert whether the hook reached it.
        bootupctl = bin_dir / "bootupctl"
        bootupctl.write_text(
            "#!/usr/bin/env bash\n"
            'mkdir -p "${HOOK_STATE_DIR}"\n'
            'echo "$@" >> "${HOOK_STATE_DIR}/bootupctl_args"\n'
            'exit "${HOOK_BOOTUPD_RC:-0}"\n'
        )
        bootupctl.chmod(0o755)

        self.env = dict(os.environ)
        self.env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
        self.env["HOOK_STATE_DIR"] = str(self.fake / "state")
        self.env["HOOK_ROOT_FSTYPE"] = "ext4"
        self.env["HOOK_BOOTUPD_RC"] = "0"

    def run_hook(self):
        """Run the real hook with a wrapper that points libsetup at the stub."""
        # The hook hardcodes /usr/lib/ublue/setup-services/libsetup.sh; rewrite
        # the source line via a temporary copy so the host's real libsetup is
        # not consulted.
        hook_src = HOOK.read_text()
        patched = hook_src.replace(
            "/usr/lib/ublue/setup-services/libsetup.sh",
                str(self.fake / "libexec" / "libsetup.sh"),
        )
        wrapper = self.fake / "hook.sh"
        wrapper.write_text(patched)
        wrapper.chmod(0o755)
        return subprocess.run(
            ["bash", str(wrapper)],
            capture_output=True, text=True, env=self.env,
        )

    def test_first_boot_runs_bootupctl_adopt_and_update(self):
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        args = (self.fake / "state" / "bootupctl_args").read_text().splitlines()
        self.assertEqual(args, ["adopt-and-update"])
        # The commit marker must exist so the hook does not retry next boot.
        self.assertTrue(
            (self.fake / "state" / "privileged-bootupctl-adopt-1").exists()
        )

    def test_second_boot_skips_adopt_and_update(self):
        # First boot completes the adoption.
        first = self.run_hook()
        self.assertEqual(first.returncode, 0, first.stderr)
        # Second boot: the version-script-check exits 1, so the hook short-
        # circuits without ever invoking bootupctl.
        second = self.run_hook()
        self.assertEqual(second.returncode, 0, second.stderr)
        args = (self.fake / "state" / "bootupctl_args").read_text().splitlines()
        self.assertEqual(args, ["adopt-and-update"])

    def test_live_session_skips_without_committing(self):
        self.env["HOOK_ROOT_FSTYPE"] = "squashfs"
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(
            (self.fake / "state" / "bootupctl_args").exists(),
            "live session must not call bootupctl",
        )
        # No commit marker, so the hook retries next boot. A live session that
        # is then installed (squashfs /sysroot no longer mounted) will run.
        self.assertFalse(
            (self.fake / "state" / "privileged-bootupctl-adopt-1").exists()
        )

    def test_live_erofs_root_also_skips(self):
        self.env["HOOK_ROOT_FSTYPE"] = "erofs"
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(
            (self.fake / "state" / "bootupctl_args").exists()
        )

    def test_missing_bootupctl_skips_without_committing(self):
        # Remove the bootupctl stub to simulate an image variant that does
        # not carry it. The hook must exit cleanly and not commit, so a later
        # bootc switch that brings bootupctl in can still adopt.
        (self.fake / "bin" / "bootupctl").unlink()
        r = self.run_hook()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(
            (self.fake / "state" / "bootupctl_args").exists()
        )
        self.assertFalse(
            (self.fake / "state" / "privileged-bootupctl-adopt-1").exists()
        )

    def test_failing_bootupctl_aborts_before_commit(self):
        # A failing adopt-and-update (no EFI dir, missing payload) must not
        # record a completion, so common#1196 retries next boot.
        self.env["HOOK_BOOTUPD_RC"] = "1"
        r = self.run_hook()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(
            (self.fake / "state" / "bootupctl_args").exists()
        )
        self.assertFalse(
            (self.fake / "state" / "privileged-bootupctl-adopt-1").exists(),
            "failed adoption must not be committed",
        )


if __name__ == "__main__":
    unittest.main()