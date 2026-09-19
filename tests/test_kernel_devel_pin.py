"""KERNEL_DEVEL_SHA256 must not outlive the kernel it was recorded for.

The koji fallback in install-nvidia.sh downloads an unsigned kernel-devel and
checks it against a hash committed in the script. The hash is a constant
because the base image is pinned by digest -- which is exactly why it has to
move when BASE_IMAGE does. The 7.2 base bump left it pinned to 7.1.8-100.fc43
for a revision: the dnf path satisfied 7.2 so the fallback never ran, nothing
failed, and nothing said the constant had gone stale. It would have failed only
once the enabled repositories dropped the kernel, which is the one situation
the fallback exists for.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/install-nvidia.sh").read_text()


def constant(name: str) -> str:
    match = re.search(rf'^{name}="(?:\$\{{[A-Z0-9_]+:-)?([^"}}]+)\}}?"', SCRIPT, re.MULTILINE)
    assert match, f"{name} not found in install-nvidia.sh"
    return match.group(1)


class KernelDevelPinTests(unittest.TestCase):
    def test_the_hash_records_which_kernel_it_belongs_to(self):
        nevr = constant("KERNEL_DEVEL_NEVR")
        self.assertRegex(nevr, r"^\d+\.\d+\.\d+-\d+\.fc\d+\.\w+$")
        self.assertRegex(constant("KERNEL_DEVEL_SHA256"), r"^[0-9a-f]{64}$")

    def test_the_pin_matches_the_kernel_the_base_image_boots(self):
        # The one assertion that actually catches the staleness: the recorded
        # NEVR has to be the kernel this base carries. Update both together.
        self.assertEqual(constant("KERNEL_DEVEL_NEVR"), "7.2.5-200.fc44.x86_64")

    def test_a_mismatched_kernel_fails_before_downloading_60mb(self):
        """Drive the real guard: a kernel that is not the recorded one exits 1."""
        guard = re.search(
            r'if \[ "\$\{kernel\}" != "\$\{KERNEL_DEVEL_NEVR\}" \]; then.*?\n      fi',
            SCRIPT, re.DOTALL)
        self.assertIsNotNone(guard, "the NEVR guard is not in install-nvidia.sh")
        harness = (
            'KERNEL_DEVEL_NEVR="7.2.5-200.fc44.x86_64"\n'
            'kernel="7.1.8-100.fc43.x86_64"\n'
            + guard.group(0).replace("\n      ", "\n")
            + '\necho "reached the download"\n'
        )
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn("reached the download", result.stdout)
        self.assertIn("7.1.8-100.fc43.x86_64", result.stderr)
        self.assertIn("7.2.5-200.fc44.x86_64", result.stderr)

    def test_the_matching_kernel_passes_the_guard(self):
        guard = re.search(
            r'if \[ "\$\{kernel\}" != "\$\{KERNEL_DEVEL_NEVR\}" \]; then.*?\n      fi',
            SCRIPT, re.DOTALL)
        harness = (
            'KERNEL_DEVEL_NEVR="7.2.5-200.fc44.x86_64"\n'
            'kernel="7.2.5-200.fc44.x86_64"\n'
            + guard.group(0).replace("\n      ", "\n")
            + '\necho "reached the download"\n'
        )
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reached the download", result.stdout)

    def test_both_constants_are_overridable_together(self):
        """The guard must not make the hash override unreachable.

        UTAH_KERNEL_DEVEL_SHA256 exists for one situation: validating a kernel
        this file has not been re-recorded for. A NEVR guard that refused to
        proceed whenever the booted kernel differed from the recorded one would
        block exactly that case, leaving the override as dead code. So the NEVR
        has to be overridable too, and the pair has to move together.
        """
        for name in ("KERNEL_DEVEL_NEVR", "KERNEL_DEVEL_SHA256"):
            with self.subTest(constant=name):
                line = next(l for l in SCRIPT.splitlines()
                            if l.startswith(f"{name}="))
                self.assertIn(f"${{UTAH_{name}:-", line,
                              f"{name} must accept an environment override")

    def test_the_guard_message_names_the_override(self):
        # A guard that stops the build has to say how to proceed deliberately,
        # or the only way past it is to edit the script.
        guard = re.search(
            r'if \[ "\$\{kernel\}" != "\$\{KERNEL_DEVEL_NEVR\}" \]; then.*?\n      fi',
            SCRIPT, re.DOTALL).group(0)
        self.assertIn("UTAH_KERNEL_DEVEL_NEVR", guard)
        self.assertIn("UTAH_KERNEL_DEVEL_SHA256", guard)

    def test_an_overridden_pair_reaches_the_download(self):
        """Drive the real guard with both overrides set to a different kernel."""
        guard = re.search(
            r'if \[ "\$\{kernel\}" != "\$\{KERNEL_DEVEL_NEVR\}" \]; then.*?\n      fi',
            SCRIPT, re.DOTALL).group(0)
        harness = (
            'KERNEL_DEVEL_NEVR="${UTAH_KERNEL_DEVEL_NEVR:-7.2.5-200.fc44.x86_64}"\n'
            'kernel="9.9.9-1.fc99.x86_64"\n'
            + guard.replace("\n      ", "\n")
            + '\necho "reached the download"\n'
        )
        import os
        env = dict(os.environ, UTAH_KERNEL_DEVEL_NEVR="9.9.9-1.fc99.x86_64")
        result = subprocess.run(["bash", "-c", harness], capture_output=True,
                                text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reached the download", result.stdout)


if __name__ == "__main__":
    unittest.main()
