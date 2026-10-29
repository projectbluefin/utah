"""The SELinux module store must live under writable /etc/selinux (#551).

Hummingbird keeps the libsemanage store under /usr/lib/selinux, which is
read-only on a booted bootc system, so `setsebool -P`, `semanage` and
`semodule` all fail there. `scripts/relocate-selinux-store.sh` moves the
store to /etc/selinux at build time, as Fedora atomic and Bluefin do, and
`iso/scripts/verify-selinux-store.sh` proves on a booted guest that the
four kinds of persistent change work and survive an update.

Host-side tests cannot run semanage, so they drive the relocation script
against scratch trees and check the two scripts' contracts statically.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELOCATE = ROOT / "scripts/relocate-selinux-store.sh"
CHECKER = ROOT / "iso/scripts/verify-selinux-store.sh"
LIFECYCLE = ROOT / "iso/scripts/lifecycle-e2e.sh"
CONTAINERFILE = ROOT / "Containerfile"
JUSTFILE = ROOT / "Justfile"

CONF_HEAD = "# Authors: Jason Tang\nmodule-store = direct\n\n[verify kernel]\n"
OLD_LINE = "store-root=/usr/lib/selinux"
NEW_LINE = "store-root=/etc/selinux"


class RelocateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = self.root / "usr/lib/selinux"
        self.new = self.root / "etc/selinux"
        self.conf = self.new / "semanage.conf"

    def hummingbird_layout(self):
        """The layout Hummingbird's base Containerfile produces."""
        (self.old / "targeted/active/modules/100/abrt").mkdir(parents=True)
        (self.old / "targeted/active/modules/100/abrt/cil").write_bytes(b"cil")
        (self.old / "targeted/active/commit_num").write_text("1\n")
        # libsemanage's store locks, taken by the package transaction that
        # runs before the relocation (CI run 38405812025).
        (self.old / "targeted/semanage.read.LOCK").touch()
        (self.old / "targeted/semanage.trans.LOCK").touch()
        (self.old / "final/targeted/contexts/files").mkdir(parents=True)
        (self.old / "final/targeted/contexts/files/file_contexts").write_text("x\n")
        (self.new / "targeted/policy").mkdir(parents=True)
        (self.new / "targeted/policy/policy.35").write_bytes(b"compiled")
        self.conf.write_text(CONF_HEAD + "\n" + OLD_LINE + "\n")

    def run_script(self):
        return subprocess.run(
            ["bash", str(RELOCATE), str(self.root)],
            capture_output=True, text=True, check=False,
        )

    def test_moves_store_and_points_semanage_conf_at_etc(self):
        self.hummingbird_layout()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.new / "targeted/active/modules/100/abrt/cil").is_file())
        self.assertTrue((self.new / "targeted/active/commit_num").is_file())
        self.assertTrue((self.new / "final/targeted/contexts/files/file_contexts").is_file())
        self.assertTrue((self.new / "targeted/semanage.read.LOCK").is_file())
        self.assertTrue((self.new / "targeted/semanage.trans.LOCK").is_file())
        self.assertFalse(self.old.exists(), "/usr/lib/selinux must not linger")
        lines = self.conf.read_text().splitlines()
        self.assertIn(NEW_LINE, lines)
        self.assertNotIn(OLD_LINE, lines)
        self.assertTrue(
            self.conf.read_text().startswith(CONF_HEAD),
            "only the store-root line may change",
        )
        # The compiled policy the kernel loads was already under /etc.
        self.assertEqual((self.new / "targeted/policy/policy.35").read_bytes(), b"compiled")

    def test_store_without_lock_files_still_relocates(self):
        """A base that never took the store lock leaves no LOCK files behind."""
        self.hummingbird_layout()
        (self.old / "targeted/semanage.read.LOCK").unlink()
        (self.old / "targeted/semanage.trans.LOCK").unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.old.exists())
        self.assertFalse((self.new / "targeted/semanage.read.LOCK").exists())

    def test_existing_lock_in_etc_wins_over_an_empty_old_one(self):
        self.hummingbird_layout()
        (self.new / "targeted/semanage.read.LOCK").touch()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.old.exists())
        self.assertTrue((self.new / "targeted/semanage.read.LOCK").is_file())

    def test_second_run_is_a_no_op(self):
        self.hummingbird_layout()
        self.assertEqual(self.run_script().returncode, 0)
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already", result.stdout)
        after = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        self.assertEqual(before, after)

    def test_unknown_store_root_fails_instead_of_guessing(self):
        self.hummingbird_layout()
        self.conf.write_text(CONF_HEAD + "\nstore-root=/var/lib/selinux\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not carry", result.stderr)
        self.assertTrue((self.old / "targeted/active").is_dir(), "nothing may move on failure")

    def test_missing_semanage_conf_fails(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("semanage.conf", result.stderr)

    def test_unexpected_files_under_old_store_fail_the_build(self):
        self.hummingbird_layout()
        (self.old / "targeted/previous").mkdir()
        (self.old / "targeted/previous/keep").write_text("?\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected entries", result.stderr)
        self.assertIn("targeted/previous", result.stderr)
        self.assertTrue(
            (self.old / "targeted/previous/keep").is_file(),
            "an rm -rf must not swallow what the base changed",
        )
        self.assertIn(OLD_LINE, self.conf.read_text().splitlines())

    def test_two_live_stores_fail(self):
        self.hummingbird_layout()
        (self.new / "targeted/active").mkdir()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("two stores", result.stderr)


class ContainerfileContractTests(unittest.TestCase):
    def test_script_is_staged_and_installed(self):
        text = CONTAINERFILE.read_text()
        self.assertIn("scripts/relocate-selinux-store.sh \\", text)
        self.assertIn(
            "relocate-selinux-store.sh:utah-relocate-selinux-store \\", text
        )

    def test_store_moves_before_the_home_label_policy_rebuild(self):
        """fix-home-labels rebuilds the policy; the store must already be in /etc."""
        text = CONTAINERFILE.read_text()
        relocate = text.index("/usr/local/libexec/utah-relocate-selinux-store")
        rebuild = text.index("/usr/local/libexec/utah-fix-home-labels && \\")
        self.assertLess(relocate, rebuild)
        run_start = text.rindex("\nRUN ", 0, relocate)
        run_end = text.index("\n\n", rebuild)
        self.assertIn("utah-install-packages", text[run_start:run_end],
                      "the relocation shares the package transaction's layer")

    def test_just_check_guards_the_scripts(self):
        text = JUSTFILE.read_text()
        self.assertIn("test -f scripts/relocate-selinux-store.sh", text)
        self.assertIn("test -f iso/scripts/verify-selinux-store.sh", text)
        self.assertIn("utah-relocate-selinux-store' Containerfile", text)


class GuestCheckerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conf = Path(self.temp.name) / "semanage.conf"

    def run_checker(self, *args):
        env = dict(os.environ, UTAH_SEMANAGE_CONF=str(self.conf))
        return subprocess.run(
            ["bash", str(CHECKER), *args],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_probe_reports_store_layout(self):
        self.conf.write_text(CONF_HEAD + NEW_LINE + "\n")
        result = self.run_checker("probe")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "persistent")
        self.conf.write_text(CONF_HEAD + OLD_LINE + "\n")
        self.assertEqual(self.run_checker("probe").stdout.strip(), "immutable")

    def test_apply_refuses_an_immutable_store(self):
        """Without a writable store the sample changes cannot be made."""
        self.conf.write_text(CONF_HEAD + OLD_LINE + "\n")
        result = self.run_checker("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("store-root", result.stderr)

    def test_unknown_mode_is_a_usage_error(self):
        result = self.run_checker("bogus")
        self.assertEqual(result.returncode, 2)

    def test_checker_covers_the_four_kinds_of_change(self):
        text = CHECKER.read_text()
        for command in (
            'setsebool -P "$BOOLEAN" 1',
            'semanage fcontext -a -t "$FCONTEXT_TYPE" "$FCONTEXT_RULE"',
            'semanage port -a -t "$PORT_TYPE" -p tcp "$PORT"',
            'semodule -i "${module_dir}/${MODULE}.cil"',
            'setsebool -P "$BOOLEAN" 0',
            'semanage fcontext -d "$FCONTEXT_RULE"',
            'semanage port -d -p tcp "$PORT"',
            'semodule -r "$MODULE"',
        ):
            self.assertIn(command, text, command)
        # The new image's policy must be running, not held back by a local store.
        self.assertIn("require_image_policy", text)
        self.assertIn("home_root_t", text)
        self.assertIn("Enforcing", text)

    def test_reject_mode_requires_a_refused_module_and_unchanged_state(self):
        text = CHECKER.read_text()
        self.assertIn("semodule accepted the invalid module", text)
        self.assertIn("require_image_policy", text[text.index("    reject)"):text.index("    *)")])
        self.conf.write_text(CONF_HEAD + OLD_LINE + "\n")
        result = self.run_checker("reject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("store-root", result.stderr)

    def test_no_quiet_grep_at_the_end_of_a_pipeline(self):
        """`grep -q` closing a pipe early trips pipefail on the producer."""
        for line in CHECKER.read_text().splitlines():
            if "| grep -q" in line:
                self.fail(f"grep -q must not consume a pipe: {line.strip()}")


class ImagePolicyCheckTests(unittest.TestCase):
    """`image-policy` against fake SELinux tools.

    The fakes answer from `<name>-image` when given the pristine compiled
    policy and from `<name>-running` otherwise, as seinfo and sesearch read
    the running policy when no policy file is named.
    """

    TYPES = "Types: 3\n   init_t\n   httpd_t\n   user_home_dir_t\n"
    RULES = (
        "allow httpd_t httpd_sys_content_t:file { getattr open read };\n"
        "type_transition init_t httpd_exec_t:process httpd_t;\n"
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.conf = self.dir / "semanage.conf"
        self.conf.write_text(CONF_HEAD + NEW_LINE + "\n")
        self.store = self.dir / "pristine"
        (self.store / "policy").mkdir(parents=True)
        (self.store / "policy/policy.35").write_bytes(b"compiled")
        (self.store / "active/modules/100/apache").mkdir(parents=True)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        for tool in ("seinfo", "sesearch"):
            self.fake(tool, f"""for arg; do
    if [[ "$arg" == "{self.store}/policy/policy.35" ]]; then
        exec cat "{self.dir}/{tool}-image"
    fi
done
exec cat "{self.dir}/{tool}-running"
""")
        self.fake("getenforce", "echo Enforcing\n")
        self.fake("semodule", "printf 'apache\\nutah_e2e\\n'\n")
        self.write("seinfo-image", self.TYPES)
        self.write("sesearch-image", self.RULES)

    def fake(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/bash\n" + body)
        path.chmod(0o755)

    def write(self, name, text):
        (self.dir / name).write_text(text)

    def run_check(self):
        env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            UTAH_SEMANAGE_CONF=str(self.conf),
            UTAH_PRISTINE_STORE=str(self.store),
        )
        return subprocess.run(
            ["bash", str(CHECKER), "image-policy"],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_image_policy_plus_the_sample_module_passes(self):
        self.write("seinfo-running", self.TYPES + "   utah_e2e_t\n")
        self.write("sesearch-running", self.RULES + "allow utah_e2e_t utah_e2e_t:file read;\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_changed_allow_rule_with_identical_names_fails(self):
        """hanthor's #894 finding: name sets alone miss a changed rule."""
        self.write("seinfo-running", self.TYPES)
        self.write("sesearch-running", self.RULES.replace(" open read", " read"))
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TE rules differ", result.stderr)
        self.assertIn("image only: allow httpd_t", result.stderr)
        self.assertIn("running only: allow httpd_t", result.stderr)

    def test_type_the_image_dropped_but_still_running_fails(self):
        self.write("seinfo-running", self.TYPES + "   old_t\n")
        self.write("sesearch-running", self.RULES)
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("types differ", result.stderr)
        self.assertIn("running only: old_t", result.stderr)

    def test_empty_tool_output_fails_instead_of_comparing_equal(self):
        self.write("seinfo-image", "")
        self.write("seinfo-running", "")
        self.write("sesearch-running", self.RULES)
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("seinfo cannot list the types", result.stderr)

    def test_many_differences_report_instead_of_dying_on_sigpipe(self):
        self.write("seinfo-running", self.TYPES)
        self.write("sesearch-running", "".join(
            f"allow t{i}_t t{i}_t:file read;\n" for i in range(20000)
        ))
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TE rules differ", result.stderr)

    def test_missing_pristine_module_fails(self):
        (self.store / "active/modules/100/extra").mkdir()
        self.write("seinfo-running", self.TYPES)
        self.write("sesearch-running", self.RULES)
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing from the active store: extra", result.stderr)

    def test_immutable_store_compares_rules_without_listing_modules(self):
        self.conf.write_text(CONF_HEAD + OLD_LINE + "\n")
        self.fake("semodule", "exit 1\n")
        self.write("seinfo-running", self.TYPES)
        self.write("sesearch-running", self.RULES)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)


class LifecycleHarnessTests(unittest.TestCase):
    def test_harness_applies_before_and_verifies_after_the_upgrade(self):
        text = LIFECYCLE.read_text()
        self.assertIn("iso/scripts/verify-selinux-store.sh", text)
        baseline_apply = text.index("selinux_check baseline apply")
        stage = text.index('ACTIVE_PHASE="staged"')
        upgraded_verify = text.index("selinux_check upgraded verify")
        upgraded_apply = text.index("selinux_check upgraded apply")
        upgraded_reject = text.index("selinux_check upgraded reject")
        upgraded_remove = text.index("selinux_check upgraded remove")
        rollback = text.index('ACTIVE_PHASE="rollback"')
        self.assertLess(baseline_apply, stage)
        self.assertLess(stage, upgraded_verify)
        self.assertLess(upgraded_verify, upgraded_apply)
        self.assertLess(upgraded_apply, upgraded_reject)
        self.assertLess(upgraded_reject, upgraded_remove)
        self.assertLess(upgraded_remove, rollback)

    def test_candidate_policy_is_checked_before_anything_rebuilds_it(self):
        text = LIFECYCLE.read_text()
        image_policy = text.index("selinux_check upgraded image-policy")
        self.assertLess(text.index('ACTIVE_PHASE="upgraded"'), image_policy)
        for rebuild in ("verify", "apply", "reject", "remove"):
            self.assertLess(image_policy, text.index(f"selinux_check upgraded {rebuild}"), rebuild)

    def test_rollback_checks_the_restored_selinux_state(self):
        text = LIFECYCLE.read_text()
        rollback = text.index('ACTIVE_PHASE="rollback"')
        complete = text.index('ACTIVE_PHASE="complete"')
        section = text[rollback:complete]
        self.assertIn('[[ "${ROLLBACK_SELINUX}" == "${BASELINE_SELINUX}" ]]', section)
        self.assertIn("selinux_check rollback image-policy", section)

    def test_upgraded_deployment_must_have_a_writable_store(self):
        text = LIFECYCLE.read_text()
        self.assertIn('[[ "${UPGRADED_SELINUX}" == "persistent" ]]', text)
        self.assertIn("diagnose_failure \"Upgraded deployment keeps its SELinux policy store", text)

    def test_probe_never_reports_from_a_subshell(self):
        """diagnose_failure's report would vanish inside `$(selinux_probe)`."""
        text = LIFECYCLE.read_text()
        start = text.index("selinux_probe() {")
        end = text.index("\n}\n", start)
        self.assertNotIn("diagnose_failure", text[start:end])


if __name__ == "__main__":
    unittest.main()
