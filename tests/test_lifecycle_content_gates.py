"""The lifecycle runs content gates on the candidate, not on the baseline.

The baseline is the previous :testing. When a new image rule lands (/efi,
#1330; the initrc_t rule for the installer's boot receipt, #1131), the
baseline still predates it; judging it by that rule failed every lifecycle
run, and since promotion needs the lifecycle, :testing could never move past
the image that lacked the rule (#1343). luks-e2e.sh skips every content gate
under UTAH_E2E_BASELINE=1, and lifecycle-e2e.sh runs each one on the upgraded
candidate.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LUKS = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
LIFECYCLE = (ROOT / "iso/scripts/lifecycle-e2e.sh").read_text()

BASELINE_SWITCH = 'BASELINE_INSTALL="${UTAH_E2E_BASELINE:-0}"\n'
EFI_GATE_START = 'if [[ "${BASELINE_INSTALL}" == 0 ]]; then\n    echo "Verifying the installed EFI'
PROBE_GATE_START = 'if [[ "${BASELINE_INSTALL}" == 0 ]]; then\n    echo "Verifying bootc\'s SELinux'
DENIAL = ('type=AVC msg=audit(1795247042.592:77): avc:  denied  { mac_admin } for  pid=2690 '
          'comm="chcon" capability=33  scontext=system_u:system_r:initrc_t:s0 '
          'tcontext=system_u:system_r:initrc_t:s0 tclass=capability2 permissive=0')


def block(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


def luks_gate(start: str) -> str:
    return BASELINE_SWITCH + block(LUKS, start, "\nfi\n") + "\nfi\n"


class LifecycleContentGateTests(unittest.TestCase):
    def run_bash(self, script: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-eu", "-c", script], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", **env})

    def luks_efi_gate(self, **env: str) -> subprocess.CompletedProcess:
        # An installed image without /efi: every ssh check fails.
        harness = ('fail() { echo "FAIL: $*" >&2; exit 1; }\n'
                   "ssh_target() { return 1; }\n" + luks_gate(EFI_GATE_START) + "echo passed\n")
        return self.run_bash(harness, **env)

    def luks_probe_gate(self, **env: str) -> subprocess.CompletedProcess:
        # An installed image whose policy lacks the rule: the AVC is logged.
        with tempfile.TemporaryDirectory() as work:
            harness = (f'WORK="{work}"\nTEST_PASSWORD=x\nTEST_USER=u\n'
                       'fail() { echo "FAIL: $*" >&2; exit 1; }\n'
                       f"ssh_target() {{ printf '%s\\n' '{DENIAL}'; }}\n"
                       "scp_target() { return 1; }\n"
                       + luks_gate(PROBE_GATE_START) + "echo passed\n")
            return self.run_bash(harness, **env)

    def test_luks_e2e_fails_an_image_without_efi_by_default(self):
        result = self.luks_efi_gate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EFI mountpoint is missing", result.stderr)

    def test_luks_e2e_fails_a_bootc_probe_denial_by_default(self):
        result = self.luks_probe_gate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("chcon was denied mac_admin", result.stderr)

    def test_baseline_install_skips_the_content_gates(self):
        for gate in (self.luks_efi_gate, self.luks_probe_gate):
            with self.subTest(gate=gate.__name__):
                result = gate(UTAH_E2E_BASELINE="1")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.endswith("passed\n"), result.stdout)

    def test_lifecycle_installs_the_baseline_with_the_switch(self):
        self.assertIn('UTAH_E2E_BASELINE=1 bash "${ROOT}/iso/scripts/luks-e2e.sh"', LIFECYCLE)
        self.assertNotIn("UTAH_E2E_EFI_CHECK", LIFECYCLE + LUKS)

    def test_upgraded_candidate_runs_every_skipped_gate(self):
        upgraded = LIFECYCLE.index("verify_boot_files upgraded\n")
        after = LIFECYCLE[upgraded:].splitlines()[1:3]
        self.assertEqual(after, ["verify_efi_mount upgraded", "verify_bootc_probe upgraded"])

    def test_upgraded_candidate_efi_check(self):
        function = block(LIFECYCLE, "verify_efi_mount() {", "\n}\n") + "\n}\n"
        for ssh_status, expect_fail in ((1, True), (0, False)):
            with self.subTest(ssh_status=ssh_status):
                harness = ('EVIDENCE="$(mktemp -d)"\n'
                           'diagnose_failure() { echo "DIAG: $*" >&2; exit 1; }\n'
                           f"ssh_target() {{ return {ssh_status}; }}\n"
                           + function + "verify_efi_mount upgraded\n")
                result = self.run_bash(harness)
                if expect_fail:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("EFI mountpoint is missing", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_upgraded_candidate_bootc_probe_check(self):
        function = block(LIFECYCLE, "verify_bootc_probe() {", "\n}\n") + "\n}\n"
        for records, expect_fail in ((DENIAL, True), ("", False)):
            with self.subTest(denied=bool(records)):
                harness = ('EVIDENCE="$(mktemp -d)"\n'
                           'diagnose_failure() { echo "DIAG: $*" >&2; exit 1; }\n'
                           f"ssh_target_sudo() {{ printf '%s' '{records}'; }}\n"
                           + function + "verify_bootc_probe upgraded\n")
                result = self.run_bash(harness)
                if expect_fail:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("chcon was denied mac_admin on the upgraded boot", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_candidate_probe_check_reads_only_the_current_boot(self):
        # audit.log keeps the baseline's first boot, whose policy may lack the
        # rule: its denial must not fail the candidate.
        function = block(LIFECYCLE, "verify_bootc_probe() {", "\n}\n")
        script = block(function, "script=\"$(cat <<'INNER'\n", "\nINNER\n")
        script = script.split("\n", 1)[1]
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp, "audit.log")
            uptime = Path(tmp, "uptime")
            old = DENIAL.replace("1795247042.592", "1795240000.100")
            new = DENIAL.replace("1795247042.592", "1795247050.200")
            audit.write_text(f"{old}\n{new}\n")
            uptime.write_text("100.25 180.00\n")
            script = (script.replace("/var/log/audit/audit.log", str(audit))
                      .replace("/proc/uptime", str(uptime)))
            harness = "date() { echo 1795247100; }\njournalctl() { :; }\n" + script + "\n"
            result = self.run_bash(harness)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, new + "\n")


if __name__ == "__main__":
    unittest.main()
