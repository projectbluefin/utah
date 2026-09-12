#!/usr/bin/env python3
"""Focused source tests for Utah's first-boot desktop contract."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import tomllib


REPO_ROOT = Path(__file__).resolve().parents[2]
TAILSCALE_HOOK = (
    REPO_ROOT
    / "system_files/shared/usr/share/ublue-os/privileged-setup.hooks.d/10-tailscale.sh"
)
CONTRACT = REPO_ROOT / "contracts/bluefin-desktop.toml"


def executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class FirstBootTests(unittest.TestCase):
    def test_contract_declares_runtime_policy(self) -> None:
        contract = tomllib.loads(CONTRACT.read_text())
        first_boot = contract["first_boot"]
        services = contract["services"]

        self.assertIn(
            "/usr/share/ublue-os/privileged-setup.hooks.d/10-tailscale.sh",
            first_boot["hooks"],
        )
        self.assertEqual(
            first_boot["deferred_state"],
            "/var/lib/ublue/setup-services/tailscale.deferred",
        )
        self.assertIn("input-remapper.service", services["enabled"])
        self.assertIn("bluefin-stats-refresh.timer", services["enabled"])
        self.assertIn("ublue-user-setup.service", services["user_enabled"])
        self.assertEqual(services["optional"], ["tailscaled.service"])

    def test_all_declared_hooks_and_runtime_checker_parse(self) -> None:
        contract = tomllib.loads(CONTRACT.read_text())
        hooks = [
            REPO_ROOT / "system_files/shared" / Path(path).relative_to("/")
            for path in contract["first_boot"]["hooks"]
        ]
        hooks.append(REPO_ROOT / "scripts/verify-first-boot.sh")
        for hook in hooks:
            with self.subTest(hook=hook):
                result = subprocess.run(
                    ["bash", "-n", str(hook)], capture_output=True, text=True, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_tailscale_missing_binary_is_deferred_without_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub_bin = root / "bin"
            stub_bin.mkdir()
            # The hook needs these two coreutils to create its marker. Keeping
            # the PATH isolated proves command -v cannot discover tailscale.
            for command in ("dirname", "install"):
                (stub_bin / command).symlink_to(f"/usr/bin/{command}")

            hook = root / "tailscale.sh"
            hook.write_text(
                TAILSCALE_HOOK.read_text().replace(
                    "source /usr/lib/ublue/setup-services/libsetup.sh",
                    "version-script() { return 0; }",
                )
            )
            hook.chmod(0o755)
            state = root / "tailscale.deferred"
            env = {
                **os.environ,
                "PATH": str(stub_bin),
                "TAILSCALE_DEFERRED_STATE": str(state),
                "PKEXEC_UID": "1000",
                "HOME": str(root / "home"),
            }
            result = subprocess.run(
                ["/usr/bin/bash", str(hook)], env=env, capture_output=True, text=True, check=False
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(state.is_file())
            state_text = state.read_text()
            self.assertIn("status=deferred", state_text)
            self.assertIn("tailscale binary is not installed", state_text)

    def test_tailscale_success_clears_a_deferred_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub_bin = root / "bin"
            stub_bin.mkdir()
            for command in ("dirname", "install"):
                (stub_bin / command).symlink_to(f"/usr/bin/{command}")
            executable(
                stub_bin / "tailscale",
                "#!/usr/bin/bash\nprintf '%s\\n' \"$*\" >\"$TAILSCALE_LOG\"\n",
            )
            executable(
                stub_bin / "getent",
                "#!/usr/bin/bash\nprintf 'testuser:x:1000:1000::/home/testuser:/bin/bash\\n'\n",
            )

            hook = root / "tailscale.sh"
            hook.write_text(
                TAILSCALE_HOOK.read_text().replace(
                    "source /usr/lib/ublue/setup-services/libsetup.sh",
                    "version-script() { return 0; }",
                )
            )
            hook.chmod(0o755)
            state = root / "tailscale.deferred"
            state.write_text("status=deferred\nreason=previously missing\n")
            log = root / "tailscale.log"
            env = {
                **os.environ,
                "PATH": f"{stub_bin}:/usr/bin:/bin",
                "TAILSCALE_DEFERRED_STATE": str(state),
                "TAILSCALE_LOG": str(log),
                "PKEXEC_UID": "1000",
                "HOME": str(root / "home"),
            }
            result = subprocess.run(
                ["/usr/bin/bash", str(hook)], env=env, capture_output=True, text=True, check=False
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state.exists())
            self.assertIn("set --operator=testuser", log.read_text())

    def test_service_script_makes_optional_policy_explicit(self) -> None:
        script = (REPO_ROOT / "scripts/configure-services.sh").read_text()
        self.assertIn("enable_optional_unit tailscaled.service", script)
        self.assertIn("enable_required_unit input-remapper.service", script)
        self.assertIn("enable_required_unit bluefin-stats-refresh.timer", script)


if __name__ == "__main__":
    unittest.main()
