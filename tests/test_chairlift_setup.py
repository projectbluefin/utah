"""ChairLift's GUI installation must be enabled, and build failures visible."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ChairLiftSetupTests(unittest.TestCase):
    def run_setup(self, exists=0, enable=0):
        source = (ROOT / "scripts/configure-services.sh").read_text()
        block = source.split("# Common's user preset is not applied", 1)[1].split(
            "# Match Bluefin's login behavior.", 1
        )[0]
        # Execute the actual required setup with controlled unit discovery and
        # systemctl failures. Bash must stop before later build operations.
        block = "# Common's user preset is not applied" + block
        return subprocess.run(
            ["bash", "-ec", f'''
user_unit_exists() {{ return {exists}; }}
systemctl() {{ printf '%s\\n' "$*"; return {enable}; }}
{block}
echo completed
'''], capture_output=True, text=True,
        )

    def test_enables_gui_installation_for_every_user(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "--global enable brew-preinstall.service", "completed",
        ])

    def test_missing_preinstaller_fails_the_build(self):
        result = self.run_setup(exists=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("completed", result.stdout)
        self.assertNotIn("--global", result.stdout)

    def test_enablement_failure_fails_the_build(self):
        result = self.run_setup(enable=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("completed", result.stdout)


if __name__ == "__main__":
    unittest.main()
