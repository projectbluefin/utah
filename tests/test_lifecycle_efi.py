"""The lifecycle gate checks /efi on the candidate, not on the baseline (#1343).

The baseline is the previous :testing. When a new image rule lands (here /efi,
#1330), the baseline still predates it; judging it by that rule failed every
lifecycle run, and since promotion needs the lifecycle, :testing could never
move past the image that lacked /efi.
"""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LUKS = (ROOT / "iso/scripts/luks-e2e.sh").read_text()
LIFECYCLE = (ROOT / "iso/scripts/lifecycle-e2e.sh").read_text()


def block(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


class LifecycleEfiTests(unittest.TestCase):
    def run_bash(self, script: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-eu", "-c", script], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", **env})

    def luks_efi_gate(self, **env: str) -> subprocess.CompletedProcess:
        gate = block(LUKS, 'if [[ -n "${UTAH_E2E_EFI_CHECK-x}" ]]; then', "\nfi\n") + "\nfi\n"
        # An installed image without /efi: every ssh check fails.
        harness = ('fail() { echo "FAIL: $*" >&2; exit 1; }\n'
                   "ssh_target() { return 1; }\n" + gate + "echo passed\n")
        return self.run_bash(harness, **env)

    def test_luks_e2e_fails_an_image_without_efi_by_default(self):
        result = self.luks_efi_gate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EFI mountpoint is missing", result.stderr)

    def test_lifecycle_baseline_install_skips_the_efi_gate(self):
        self.assertIn('UTAH_E2E_EFI_CHECK="" bash "${ROOT}/iso/scripts/luks-e2e.sh"', LIFECYCLE)
        result = self.luks_efi_gate(UTAH_E2E_EFI_CHECK="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "passed\n")

    def test_upgraded_candidate_is_checked(self):
        upgraded = LIFECYCLE.index("verify_boot_files upgraded")
        self.assertEqual(LIFECYCLE.index("verify_efi_mount upgraded", upgraded),
                         LIFECYCLE.index("\n", upgraded) + 1)
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


if __name__ == "__main__":
    unittest.main()
