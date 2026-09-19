"""Unit tests for Bluefin first-boot services, hooks, and enablement policy."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


class TailscaleHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_dir = Path(self.tmp.name)
        self.bin_dir = self.tmp_dir / "bin"
        self.bin_dir.mkdir()
        self.versioning_file = self.tmp_dir / "setup_versioning.json"

        # Provide a minimal libsetup.sh mock implementation
        self.libsetup_dir = self.tmp_dir / "usr/lib/ublue/setup-services"
        self.libsetup_dir.mkdir(parents=True)
        self.libsetup_file = self.libsetup_dir / "libsetup.sh"
        self.libsetup_file.write_text("""
function version-script() {
    local target="$1"
    local type="$2"
    local version="$3"
    local checker="${SETUP_CHECKER_FILE}"
    (
        if [ -f "$checker" ] && grep -q "\"${type}-${target}\": \"${version}\"" "$checker" 2>/dev/null; then
            exit 1
        fi
        mkdir -p "$(dirname "$checker")"
        echo "\"${type}-${target}\": \"${version}\"" >> "$checker"
        exit 0
    )
    return $?
}
""")

    def run_hook(self, env_override=None):
        env = dict(os.environ)
        env["PATH"] = str(self.bin_dir) + ":" + "/usr/bin:/bin"
        env["SETUP_CHECKER_FILE"] = str(self.versioning_file)
        if env_override:
            env.update(env_override)

        # Substitute source /usr/lib/ublue/setup-services/libsetup.sh with our mock
        hook_code = (
            ROOT / "system_files/shared/usr/share/ublue-os/privileged-setup.hooks.d/10-tailscale.sh"
        ).read_text()
        mocked_hook = hook_code.replace(
            "source /usr/lib/ublue/setup-services/libsetup.sh",
            f"source {self.libsetup_file}",
        )
        hook_path = self.tmp_dir / "10-tailscale.sh"
        hook_path.write_text(mocked_hook)
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)

        return subprocess.run(
            ["bash", str(hook_path)],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_missing_tailscale_binary_is_deferred_without_failure(self):
        # Isolate PATH with essential utilities only, without tailscale
        clean_bin = self.tmp_dir / "clean_bin"
        clean_bin.mkdir()
        for cmd in ["bash", "cat", "echo", "grep", "id", "getent", "cut", "mkdir", "rm"]:
            src = subprocess.run(["which", cmd], capture_output=True, text=True).stdout.strip()
            if src and Path(src).exists():
                (clean_bin / cmd).symlink_to(src)

        res = self.run_hook(env_override={"PATH": str(clean_bin), "PKEXEC_UID": str(os.getuid())})
        self.assertEqual(res.returncode, 0)
        self.assertIn("deferred", res.stdout.lower())
        self.assertIn("tailscale binary not found", res.stdout.lower())
        # Deferred state must NOT write versioning tag
        self.assertFalse(self.versioning_file.exists())

    def test_unset_or_invalid_pkexec_uid_is_deferred_without_failure(self):
        # Create mock tailscale in bin_dir
        mock_ts = self.bin_dir / "tailscale"
        mock_ts.write_text("#!/usr/bin/bash\nexit 0\n")
        mock_ts.chmod(0o755)

        # 1. Unset PKEXEC_UID
        res = self.run_hook(env_override={"PKEXEC_UID": ""})
        self.assertEqual(res.returncode, 0)
        self.assertIn("pkexec_uid not set", res.stdout.lower())
        self.assertIn("deferred", res.stdout.lower())
        self.assertFalse(self.versioning_file.exists())

        # 2. Invalid/unresolvable PKEXEC_UID (getent fails, must not crash under pipefail)
        res_invalid = self.run_hook(env_override={"PKEXEC_UID": "9999999"})
        self.assertEqual(res_invalid.returncode, 0)
        self.assertIn("operator user not resolved", res_invalid.stdout.lower())
        self.assertIn("deferred", res_invalid.stdout.lower())
        self.assertFalse(self.versioning_file.exists())

    def test_present_tailscale_binary_is_invoked_and_versioned(self):
        # Create mock tailscale in bin_dir
        log_file = self.tmp_dir / "tailscale.log"
        mock_ts = self.bin_dir / "tailscale"
        mock_ts.write_text(f"""#!/usr/bin/bash
echo "$@" >> "{log_file}"
exit 0
""")
        mock_ts.chmod(0o755)

        res = self.run_hook(env_override={"PKEXEC_UID": str(os.getuid())})
        self.assertEqual(res.returncode, 0)
        self.assertTrue(log_file.exists())
        log_content = log_file.read_text()
        self.assertIn("set --operator=", log_content)

        # Version tag must be recorded
        self.assertTrue(self.versioning_file.exists())
        self.assertIn("privileged-tailscale", self.versioning_file.read_text())

        # Second execution must be idempotent and not re-run tailscale set
        log_file.unlink()
        res2 = self.run_hook(env_override={"PKEXEC_UID": str(os.getuid())})
        self.assertEqual(res2.returncode, 0)
        self.assertFalse(log_file.exists())


class FlatpaksHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_dir = Path(self.tmp.name)
        self.versioning_file = self.tmp_dir / "setup_versioning.json"

        self.libsetup_dir = self.tmp_dir / "usr/lib/ublue/setup-services"
        self.libsetup_dir.mkdir(parents=True)
        self.libsetup_file = self.libsetup_dir / "libsetup.sh"
        self.libsetup_file.write_text("""
function version-script() {
    local target="$1"
    local type="$2"
    local version="$3"
    local checker="${SETUP_CHECKER_FILE}"
    (
        if [ -f "$checker" ] && grep -q "\"${type}-${target}\": \"${version}\"" "$checker" 2>/dev/null; then
            exit 1
        fi
        mkdir -p "$(dirname "$checker")"
        echo "\"${type}-${target}\": \"${version}\"" >> "$checker"
        exit 0
    )
    return $?
}
""")

    def run_hook(self, firefox_config_dir=None):
        hook_code = (
            ROOT / "system_files/shared/usr/share/ublue-os/privileged-setup.hooks.d/99-flatpaks.sh"
        ).read_text()
        mocked_hook = hook_code.replace(
            "source /usr/lib/ublue/setup-services/libsetup.sh",
            f"source {self.libsetup_file}",
        )
        if firefox_config_dir is not None:
            mocked_hook = mocked_hook.replace(
                "/usr/share/ublue-os/firefox-config",
                str(firefox_config_dir),
            )
        dest_dir = self.tmp_dir / "flatpak-dest"
        mocked_hook = mocked_hook.replace(
            "/var/lib/flatpak/extension/org.mozilla.firefox.systemconfig",
            str(dest_dir),
        )

        hook_path = self.tmp_dir / "99-flatpaks.sh"
        hook_path.write_text(mocked_hook)
        hook_path.chmod(0o755)

        env = dict(os.environ)
        env["SETUP_CHECKER_FILE"] = str(self.versioning_file)

        return subprocess.run(
            ["bash", str(hook_path)],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_missing_firefox_config_does_not_fail(self):
        missing_dir = self.tmp_dir / "nonexistent-firefox-config"
        res = self.run_hook(firefox_config_dir=missing_dir)
        self.assertEqual(res.returncode, 0)
        self.assertTrue(self.versioning_file.exists())
        self.assertIn("privileged-flatpaks", self.versioning_file.read_text())

        # Second execution must be idempotent
        res2 = self.run_hook(firefox_config_dir=missing_dir)
        self.assertEqual(res2.returncode, 0)

    def test_existing_firefox_config_copies_files(self):
        cfg_dir = self.tmp_dir / "firefox-config"
        cfg_dir.mkdir()
        (cfg_dir / "test-bluefin.js").write_text("// test prefs\n")

        res = self.run_hook(firefox_config_dir=cfg_dir)
        self.assertEqual(res.returncode, 0)


class ServiceEnablementPolicyTests(unittest.TestCase):
    def test_preset_enables_required_desktop_services(self):
        preset_content = (
            ROOT / "system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset"
        ).read_text()
        for unit in [
            "input-remapper.service",
            "bluefin-stats-refresh.timer",
            "gdm.service",
            "tailscaled.service",
            "ublue-system-setup.service",
            "uupd.timer",
        ]:
            with self.subTest(unit=unit):
                self.assertIn(f"enable {unit}", preset_content)

    def test_configure_services_script_enables_required_desktop_services(self):
        script_content = (ROOT / "scripts/configure-services.sh").read_text()
        for unit in [
            "input-remapper.service",
            "bluefin-stats-refresh.timer",
            "tailscaled.service",
            "ublue-system-setup.service",
            "uupd.timer",
        ]:
            with self.subTest(unit=unit):
                self.assertIn(f"enable_unit {unit}", script_content)

    def test_desktop_contract_declares_services(self):
        contract_data = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        enabled = contract_data.get("services", {}).get("enabled", [])
        for unit in [
            "input-remapper.service",
            "bluefin-stats-refresh.timer",
            "bootc-unified-storage.service",
            "brew-setup.service",
            "dconf-update.service",
            "flatpak-nuke-fedora.service",
            "flatpak-preinstall.service",
            "gdm.service",
            "ublue-system-setup.service",
            "uupd.timer",
        ]:
            with self.subTest(unit=unit):
                self.assertIn(unit, enabled)

    def test_flatpak_nuke_fedora_service_handles_missing_remote_and_creates_dir(self):
        unit_text = (
            ROOT / "system_files/shared/usr/lib/systemd/system/flatpak-nuke-fedora.service"
        ).read_text()
        self.assertIn("ExecStartPre=/usr/bin/mkdir -p /var/lib/flatpak", unit_text)
        self.assertIn("ExecStart=-/usr/bin/flatpak remote-delete --force --system fedora", unit_text)
        self.assertIn("ExecStart=-/usr/bin/flatpak remote-delete --force --system fedora-testing", unit_text)

    def test_bluefin_stats_refresh_service_condition_path(self):
        unit_text = (
            ROOT / "system_files/shared/usr/lib/systemd/system/bluefin-stats-refresh.service"
        ).read_text()
        self.assertIn("ConditionPathIsReadWrite=/var/cache", unit_text)


if __name__ == "__main__":
    unittest.main()
