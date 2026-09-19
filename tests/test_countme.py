"""Unit tests for the utah-countme client script and systemd units."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "system_files/shared/usr/libexec/utah-countme"
SERVICE_PATH = ROOT / "system_files/shared/usr/lib/systemd/system/utah-countme.service"
TIMER_PATH = ROOT / "system_files/shared/usr/lib/systemd/system/utah-countme.timer"
PRESET_PATH = ROOT / "system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset"
CONTRACT_PATH = ROOT / "contracts/bluefin-desktop.toml"
SERVICES_SCRIPT = ROOT / "scripts/configure-services.sh"


class CountmeUnitTest(unittest.TestCase):
    def test_units_exist_and_match_contract(self):
        self.assertTrue(SCRIPT_PATH.is_file())
        self.assertTrue(os.access(SCRIPT_PATH, os.X_OK))
        self.assertTrue(SERVICE_PATH.is_file())
        self.assertTrue(TIMER_PATH.is_file())

        service_text = SERVICE_PATH.read_text()
        self.assertIn("ConditionPathExists=!/etc/projectbluefin/countme/disabled", service_text)
        self.assertIn("ConditionPathExists=/run/ostree-booted", service_text)
        self.assertIn("ConditionPathExists=/usr/share/ublue-os/image-info.json", service_text)
        self.assertIn("DynamicUser=yes", service_text)
        self.assertIn("StateDirectory=utah-countme", service_text)
        self.assertIn("ExecStart=/usr/libexec/utah-countme", service_text)

        timer_text = TIMER_PATH.read_text()
        self.assertIn("ConditionPathExists=!/etc/projectbluefin/countme/disabled", timer_text)
        self.assertIn("ConditionPathExists=/run/ostree-booted", timer_text)
        self.assertIn("ConditionPathExists=/usr/share/ublue-os/image-info.json", timer_text)
        self.assertIn("OnCalendar=weekly", timer_text)
        self.assertIn("Persistent=true", timer_text)

        preset_text = PRESET_PATH.read_text()
        self.assertIn("enable utah-countme.timer", preset_text)

        contract_text = CONTRACT_PATH.read_text()
        self.assertIn('"utah-countme.timer"', contract_text)

        services_script_text = SERVICES_SCRIPT.read_text()
        self.assertIn("enable_unit utah-countme.timer", services_script_text)

    def test_opt_out_file_skips_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            disabled_file = tmp_path / "disabled"
            disabled_file.touch()

            mock_bin = tmp_path / "bin"
            mock_bin.mkdir()
            curl_mock = mock_bin / "curl"
            curl_mock.write_text("#!/bin/sh\nexit 1\n")
            curl_mock.chmod(0o755)

            state_dir = tmp_path / "state"

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin}:{env['PATH']}"
            env["DISABLED_FILE"] = str(disabled_file)
            env["STATE_DIRECTORY"] = str(state_dir)

            res = subprocess.run([str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertFalse((state_dir / "lastrun").exists())

    def test_first_run_creates_epoch_and_pings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            mock_bin = tmp_path / "bin"
            mock_bin.mkdir()

            curl_args_log = tmp_path / "curl_args.txt"
            curl_mock = mock_bin / "curl"
            curl_mock.write_text(f'#!/bin/sh\necho "$@" > "{curl_args_log}"\nexit 0\n')
            curl_mock.chmod(0o755)

            image_info = tmp_path / "image-info.json"
            image_info.write_text(
                json.dumps(
                    {
                        "image-name": "utah",
                        "image-flavor": "nvidia",
                        "image-tag": "testing-20260911",
                    }
                )
            )

            os_release = tmp_path / "os-release"
            os_release.write_text('VERSION_ID="43"\n')

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin}:{env['PATH']}"
            env["STATE_DIRECTORY"] = str(state_dir)
            env["IMAGE_INFO"] = str(image_info)
            env["OS_RELEASE_FILE"] = str(os_release)
            env["DISABLED_FILE"] = str(tmp_path / "nonexistent-disabled")

            res = subprocess.run([str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertTrue((state_dir / "epoch").exists())
            self.assertTrue((state_dir / "lastrun").exists())

            args = curl_args_log.read_text()
            self.assertIn("https://countme.projectbluefin.io/metalink", args)
            self.assertIn("repo=utah", args)
            self.assertIn("tag=testing-20260911", args)
            self.assertIn("flavor=nvidia", args)
            self.assertIn("countme=1", args)
            self.assertIn("utah-countme", args)

    def test_throttling_skips_within_7_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            mock_bin = tmp_path / "bin"
            mock_bin.mkdir()

            curl_invoked = tmp_path / "invoked.txt"
            curl_mock = mock_bin / "curl"
            curl_mock.write_text(f'#!/bin/sh\ntouch "{curl_invoked}"\nexit 0\n')
            curl_mock.chmod(0o755)

            now = int(time.time())
            (state_dir / "epoch").write_text(str(now - 100))
            (state_dir / "lastrun").write_text(str(now - 86400))

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin}:{env['PATH']}"
            env["STATE_DIRECTORY"] = str(state_dir)
            env["DISABLED_FILE"] = str(tmp_path / "nonexistent-disabled")

            res = subprocess.run([str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertFalse(curl_invoked.exists())

    def test_age_cohort_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mock_bin = tmp_path / "bin"
            mock_bin.mkdir()

            curl_args_log = tmp_path / "curl_args.txt"
            curl_mock = mock_bin / "curl"
            curl_mock.write_text(f'#!/bin/sh\necho "$@" > "{curl_args_log}"\nexit 0\n')
            curl_mock.chmod(0o755)

            image_info = tmp_path / "image-info.json"
            image_info.write_text(json.dumps({"image-name": "utah", "image-flavor": "main", "image-tag": "latest"}))

            week_secs = 604800
            now = int(time.time())

            test_cases = [
                (0, 1),              # 0 weeks -> bucket 1
                (week_secs * 2, 2),  # 2 weeks -> bucket 2 (2-4w)
                (week_secs * 6, 3),  # 6 weeks -> bucket 3 (5-24w)
                (week_secs * 30, 4), # 30 weeks -> bucket 4 (>24w)
            ]

            for offset_secs, expected_bucket in test_cases:
                with self.subTest(bucket=expected_bucket):
                    state_dir = tmp_path / f"state_{expected_bucket}"
                    state_dir.mkdir()
                    (state_dir / "epoch").write_text(str(now - offset_secs))

                    env = os.environ.copy()
                    env["PATH"] = f"{mock_bin}:{env['PATH']}"
                    env["STATE_DIRECTORY"] = str(state_dir)
                    env["IMAGE_INFO"] = str(image_info)
                    env["DISABLED_FILE"] = str(tmp_path / "nonexistent-disabled")

                    res = subprocess.run([str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
                    self.assertEqual(res.returncode, 0)
                    args = curl_args_log.read_text()
                    self.assertIn(f"countme={expected_bucket}", args)

    def test_corrupt_epoch_skips_telemetry_without_resetting(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            (state_dir / "epoch").write_text("not-a-number")

            mock_bin = tmp_path / "bin"
            mock_bin.mkdir()
            curl_invoked = tmp_path / "invoked.txt"
            curl_mock = mock_bin / "curl"
            curl_mock.write_text(f'#!/bin/sh\ntouch "{curl_invoked}"\nexit 0\n')
            curl_mock.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin}:{env['PATH']}"
            env["STATE_DIRECTORY"] = str(state_dir)
            env["DISABLED_FILE"] = str(tmp_path / "nonexistent-disabled")

            res = subprocess.run([str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertFalse(curl_invoked.exists())
            self.assertEqual((state_dir / "epoch").read_text(), "not-a-number")
            self.assertFalse((state_dir / "lastrun").exists())

    def test_curl_failure_still_updates_lastrun_to_prevent_retry_storm(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            mock_bin = tmp_path / "bin"
            mock_bin.mkdir()

            curl_mock = mock_bin / "curl"
            curl_mock.write_text("#!/bin/sh\nexit 1\n")
            curl_mock.chmod(0o755)

            image_info = tmp_path / "image-info.json"
            image_info.write_text(json.dumps({"image-name": "utah", "image-flavor": "main", "image-tag": "latest"}))

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin}:{env['PATH']}"
            env["STATE_DIRECTORY"] = str(state_dir)
            env["IMAGE_INFO"] = str(image_info)
            env["DISABLED_FILE"] = str(tmp_path / "nonexistent-disabled")

            res = subprocess.run([str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertTrue((state_dir / "epoch").exists())
            self.assertTrue((state_dir / "lastrun").exists())

    def test_os_release_fallback_when_image_info_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            mock_bin = tmp_path / "bin"
            mock_bin.mkdir()

            curl_args_log = tmp_path / "curl_args.txt"
            curl_mock = mock_bin / "curl"
            curl_mock.write_text(f'#!/bin/sh\necho "$@" > "{curl_args_log}"\nexit 0\n')
            curl_mock.chmod(0o755)

            os_release = tmp_path / "os-release"
            os_release.write_text(
                'VERSION_ID="43"\n'
                'IMAGE_VERSION="testing-osrelease"\n'
                'IMAGE_FLAVOR="gaming"\n'
            )

            env = os.environ.copy()
            env["PATH"] = f"{mock_bin}:{env['PATH']}"
            env["STATE_DIRECTORY"] = str(state_dir)
            env["IMAGE_INFO"] = str(tmp_path / "nonexistent-image-info")
            env["OS_RELEASE_FILE"] = str(os_release)
            env["DISABLED_FILE"] = str(tmp_path / "nonexistent-disabled")

            res = subprocess.run([str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            args = curl_args_log.read_text()
            self.assertIn("repo=utah", args)
            self.assertIn("tag=testing-osrelease", args)
            self.assertIn("flavor=gaming", args)
            self.assertIn("countme=1", args)


if __name__ == "__main__":
    unittest.main()
