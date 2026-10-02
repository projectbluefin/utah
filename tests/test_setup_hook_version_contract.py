"""`20-home-labels.sh` must stamp completion only after its body succeeds.

`projectbluefin/common#1196` splits `version-script` into a read-only check and
a commit: the legacy helper records the version *before* the body runs, so a
hook that fails on first boot is skipped forever after.  `20-home-labels.sh`
was the last Utah hook still written against the legacy contract, so it gets
its own assertions rather than a tree-wide rule -- the remaining hooks are
covered by #259.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = (
    ROOT
    / "system_files/shared/usr/share/ublue-os/privileged-setup.hooks.d"
    / "20-home-labels.sh"
)


class HomeLabelsHookContractTests(unittest.TestCase):
    def setUp(self):
        self.lines = [
            line.strip()
            for line in HOOK.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_hook_exists(self):
        self.assertTrue(HOOK.is_file())

    def test_does_not_use_the_legacy_burn_before_body_gate(self):
        self.assertNotIn(
            "version-script home-labels privileged 2",
            self.lines,
            "the legacy helper stamps completion before the body runs",
        )

    def test_checks_before_it_acts(self):
        check = "version-script-check home-labels privileged 2 || exit 0"
        self.assertIn(check, self.lines)
        self.assertLess(
            self.lines.index(check), self.lines.index("restorecon -RF /var/home")
        )

    def test_version_was_bumped_past_the_silent_no_op_bug(self):
        # #474: machines that ran the version-1 hook before this fix recorded
        # success even when restorecon was a no-op against a mis-keyed
        # file_contexts.homedirs. Bumping the version forces them to retry
        # once under the repaired contract.
        self.assertNotIn("home-labels privileged 1", HOOK.read_text())

    def test_commits_after_the_body(self):
        self.assertIn("version-script-commit home-labels privileged 2", self.lines)
        self.assertLess(
            self.lines.index("restorecon -RF /var/home"),
            self.lines.index("version-script-commit home-labels privileged 2"),
        )

    def test_body_failure_aborts_before_the_commit(self):
        # `set -e` is what makes a failing restorecon stop the hook instead of
        # running on to record a completion that never happened.
        self.assertIn("set -xeuo pipefail", self.lines)
        self.assertLess(
            self.lines.index("set -xeuo pipefail"),
            self.lines.index("restorecon -RF /var/home"),
        )

    def test_compat_shim_covers_pre_1196_libsetup(self):
        # The pinned COMMON_IMAGE_SHA has no version-script-check/-commit yet,
        # so the shim keeps the hook working under both contracts in either
        # merge order.
        body = HOOK.read_text()
        self.assertIn("if ! declare -F version-script-check >/dev/null; then", body)
        self.assertIn("version-script-commit() { :; }", body)

    def test_repairs_a_mis_keyed_active_homedirs_before_restorecon(self):
        # #474: file_contexts.subs_dist aliases /var/home -> /home, so a rule
        # keyed on /var/home is unreachable. If the active homedirs file
        # disagrees with the image's own /home-keyed default, restorecon is a
        # silent no-op; the hook must detect and repair that first.
        body = HOOK.read_text()
        self.assertIn("active_homedirs=", body)
        self.assertIn("pristine_homedirs=", body)
        self.assertLess(
            body.index("pristine_homedirs"), body.index("restorecon -RF /var/home")
        )

    def test_verifies_the_real_on_disk_label_before_committing(self):
        # restorecon's exit code does not prove anything was relabelled: a
        # mis-keyed database lets it finish successfully with nothing to do.
        # The hook must check the actual context on disk before recording
        # success.
        body = HOOK.read_text()
        self.assertIn("stat -c '%C' /var/home", body)
        self.assertLess(
            body.index("stat -c '%C' /var/home"),
            body.index("version-script-commit home-labels privileged 2"),
        )
        self.assertIn("home_root_t", body)


if __name__ == "__main__":
    unittest.main()
