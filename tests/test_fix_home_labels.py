"""fix-home-labels.sh must leave /var/home labelled so accounts-daemon can create users.

The script runs twice in the image build: once after the package install to
set HOME=/home and regenerate the home rules, and once with --check at the
image's last step to prove an ostree-style policy rebuild leaves those rules
unchanged (#261, #474, #575). The image build only ever exercises the path
where everything is already right, so the three guards that stop a broken
image from shipping -- a wrong HOME in /etc/default/useradd, a rebuild that
rewrites file_contexts.homedirs, and a home directory that labels as anything
but home_root_t / user_home_dir_t -- never execute in CI.

These drive the real script with stub semodule and matchpathcon. The
absolute paths it edits are pointed at a temporary tree, so the test never
touches the host's /etc.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/fix-home-labels.sh"
USERADD = "/etc/default/useradd"
HOMEDIRS = "/etc/selinux/targeted/contexts/files/file_contexts.homedirs"
GOOD_RULES = "/home/[^/]+ -d system_u:object_r:user_home_dir_t:s0\n"
LABELS = {
    "/var/home": "system_u:object_r:home_root_t:s0",
    "/var/home/someone": "system_u:object_r:user_home_dir_t:s0",
}


class FixHomeLabelsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.calls = self.tmp / "calls"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.useradd = self.tmp / "useradd"
        self.homedirs = self.tmp / "file_contexts.homedirs"
        self.rebuilt = self.tmp / "rebuilt.homedirs"

        text = SCRIPT.read_text()
        self.assertIn(USERADD, text)
        self.assertIn(HOMEDIRS, text)
        self.script = self.tmp / "fix-home-labels.sh"
        self.script.write_text(text.replace(HOMEDIRS, str(self.homedirs))
                               .replace(USERADD, str(self.useradd)))

        self.useradd.write_text("GROUP=100\nHOME=/home\nSHELL=/bin/bash\n")
        self.homedirs.write_text(GOOD_RULES)
        self.rebuilt.write_text(GOOD_RULES)
        self.set_labels(LABELS)
        # `semodule -N --refresh` stands in for ostree's per-deployment
        # rebuild: it overwrites file_contexts.homedirs with whatever the
        # rebuilt policy would produce.
        self.stub("semodule",
                  f'if [ "$1" = "-N" ]; then cp "{self.rebuilt}" "{self.homedirs}"; fi')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >>"{self.calls}"\n{body}\n')
        path.chmod(0o755)

    def set_labels(self, labels):
        cases = "".join(f'  "{p}") echo "{p}	{ctx}" ;;\n' for p, ctx in labels.items())
        # matchpathcon -m dir PATH prints "PATH<TAB>CONTEXT".
        self.stub("matchpathcon",
                  f'case "$3" in\n{cases}  *) echo "$3	<<none>>" ;;\nesac')

    def run_script(self, *args):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                   TMPDIR=str(self.tmp))
        return subprocess.run(["bash", str(self.script), *args],
                              capture_output=True, text=True, env=env)

    def called(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    # -- fix mode (no argument) ------------------------------------------

    def test_fix_rewrites_hummingbird_home_and_regenerates_rules(self):
        self.useradd.write_text("GROUP=100\nHOME=/var/home\nSHELL=/bin/bash\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.useradd.read_text(),
                         "GROUP=100\nHOME=/home\nSHELL=/bin/bash\n")
        # -n: an image build has no selinuxfs, so the policy must not load.
        self.assertIn("semodule -n -B", self.called())
        self.assertIn("/var/home: system_u:object_r:home_root_t:s0", result.stdout)
        self.assertIn("/var/home/someone: system_u:object_r:user_home_dir_t:s0",
                      result.stdout)

    def test_fix_leaves_other_home_values_alone_and_refuses_them(self):
        # The sed only rewrites the exact Hummingbird default; anything else
        # survives it and must fail the build rather than ship.
        self.useradd.write_text("HOME=/var/home/\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.useradd.read_text(), "HOME=/var/home/\n")
        self.assertIn("does not say HOME=/home", result.stderr)
        self.assertFalse(any(c.startswith("matchpathcon") for c in self.called()))

    def test_fix_refuses_useradd_without_home(self):
        self.useradd.write_text("GROUP=100\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not say HOME=/home", result.stderr)

    def test_fix_does_not_run_the_deployment_rebuild(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("semodule -N --refresh", self.called())

    # -- label guard (both modes) ----------------------------------------

    def test_home_root_labelled_default_t_fails(self):
        self.set_labels({**LABELS, "/var/home": "system_u:object_r:default_t:s0"})
        for args in ((), ("--check",)):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("/var/home labels as system_u:object_r:default_t:s0, "
                              "want home_root_t", result.stderr)
                self.assertIn("accounts-daemon cannot create users", result.stderr)

    def test_user_home_with_wrong_label_fails(self):
        self.set_labels({**LABELS, "/var/home/someone": "system_u:object_r:home_root_t:s0"})
        result = self.run_script("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/var/home/someone labels as system_u:object_r:home_root_t:s0, "
                      "want user_home_dir_t", result.stderr)

    def test_unlabelled_path_fails(self):
        self.set_labels({"/var/home/someone": LABELS["/var/home/someone"]})
        result = self.run_script("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/var/home labels as <<none>>", result.stderr)

    # -- --check mode -------------------------------------------------------

    def test_check_passes_when_rebuild_is_stable(self):
        before = self.useradd.read_text()
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called()[0], "semodule -N --refresh")
        self.assertNotIn("semodule -n -B", self.called())
        # --check only verifies; it must not edit useradd.
        self.assertEqual(self.useradd.read_text(), before)
        # The snapshot of the shipped rules is removed on success.
        self.assertEqual(list(self.tmp.glob("tmp.*")), [])

    def test_check_does_not_repair_hummingbird_home(self):
        self.useradd.write_text("HOME=/var/home\n")
        result = self.run_script("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.useradd.read_text(), "HOME=/var/home\n")
        self.assertIn("does not say HOME=/home", result.stderr)
        self.assertEqual(self.called(), [])

    def test_check_fails_when_rebuild_rewrites_home_rules(self):
        self.rebuilt.write_text("/var/home/[^/]+ -d system_u:object_r:user_home_dir_t:s0\n")
        result = self.run_script("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("a policy rebuild changes", result.stderr)
        self.assertIn("every update would rewrite the home labels", result.stderr)
        # The diff excerpt names the drifted rule so the log explains itself.
        self.assertIn("/var/home/[^/]+", result.stderr)
        self.assertFalse(any(c.startswith("matchpathcon") for c in self.called()))
        self.assertEqual(list(self.tmp.glob("tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
