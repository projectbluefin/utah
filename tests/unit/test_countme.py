"""Unit tests for the utah-countme client script and systemd units."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "system_files/shared/usr/libexec/utah-countme"
SERVICE_PATH = REPO_ROOT / "system_files/shared/usr/lib/systemd/system/utah-countme.service"
TIMER_PATH = REPO_ROOT / "system_files/shared/usr/lib/systemd/system/utah-countme.timer"
PRESET_PATH = REPO_ROOT / "system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset"


def test_units_exist_and_match_contract():
    assert SCRIPT_PATH.is_file()
    assert os.access(SCRIPT_PATH, os.X_OK)
    assert SERVICE_PATH.is_file()
    assert TIMER_PATH.is_file()

    service_text = SERVICE_PATH.read_text()
    assert "ConditionPathExists=!/etc/projectbluefin/countme/disabled" in service_text
    assert "ConditionPathExists=/run/ostree-booted" in service_text
    assert "ConditionPathExists=/usr/share/ublue-os/image-info.json" in service_text
    assert "DynamicUser=yes" in service_text
    assert "StateDirectory=utah-countme" in service_text
    assert "ExecStart=/usr/libexec/utah-countme" in service_text

    timer_text = TIMER_PATH.read_text()
    assert "ConditionPathExists=!/etc/projectbluefin/countme/disabled" in timer_text
    assert "ConditionPathExists=/run/ostree-booted" in timer_text
    assert "ConditionPathExists=/usr/share/ublue-os/image-info.json" in timer_text
    assert "OnCalendar=weekly" in timer_text
    assert "Persistent=true" in timer_text

    preset_text = PRESET_PATH.read_text()
    assert "enable utah-countme.timer" in preset_text


def test_opt_out_file_skips_execution(tmp_path):
    disabled_file = tmp_path / "disabled"
    disabled_file.touch()

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    # Mock curl that fails if called
    curl_mock = mock_bin / "curl"
    curl_mock.write_text("#!/bin/sh\nexit 1\n")
    curl_mock.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{mock_bin}:{env['PATH']}"
    env["DISABLED_FILE"] = str(disabled_file)
    env["STATE_DIRECTORY"] = str(tmp_path / "state")

    res = subprocess.run([str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert not (tmp_path / "state" / "lastrun").exists()


def test_first_run_creates_epoch_and_pings(tmp_path):
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
    assert res.returncode == 0
    assert (state_dir / "epoch").exists()
    assert (state_dir / "lastrun").exists()

    args = curl_args_log.read_text()
    assert "https://countme.projectbluefin.io/metalink" in args
    assert "repo=utah" in args
    assert "tag=testing-20260911" in args
    assert "flavor=nvidia" in args
    assert "countme=1" in args
    assert "utah-countme" in args


def test_throttling_skips_within_7_days(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()

    # If curl is called, it will write to this file
    curl_invoked = tmp_path / "invoked.txt"
    curl_mock = mock_bin / "curl"
    curl_mock.write_text(f'#!/bin/sh\ntouch "{curl_invoked}"\nexit 0\n')
    curl_mock.chmod(0o755)

    import time
    now = int(time.time())
    (state_dir / "epoch").write_text(str(now - 100))
    # Last run was 1 day ago (less than 7 days)
    (state_dir / "lastrun").write_text(str(now - 86400))

    env = os.environ.copy()
    env["PATH"] = f"{mock_bin}:{env['PATH']}"
    env["STATE_DIRECTORY"] = str(state_dir)
    env["DISABLED_FILE"] = str(tmp_path / "nonexistent-disabled")

    res = subprocess.run([str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert not curl_invoked.exists()


def test_age_cohort_buckets(tmp_path):
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()

    curl_args_log = tmp_path / "curl_args.txt"
    curl_mock = mock_bin / "curl"
    curl_mock.write_text(f'#!/bin/sh\necho "$@" > "{curl_args_log}"\nexit 0\n')
    curl_mock.chmod(0o755)

    image_info = tmp_path / "image-info.json"
    image_info.write_text(json.dumps({"image-name": "utah", "image-flavor": "main", "image-tag": "latest"}))

    week_secs = 604800
    import time
    now = int(time.time())

    test_cases = [
        (0, 1),              # 0 weeks -> bucket 1
        (week_secs * 2, 2),  # 2 weeks -> bucket 2 (2-4w)
        (week_secs * 6, 3),  # 6 weeks -> bucket 3 (5-24w)
        (week_secs * 30, 4), # 30 weeks -> bucket 4 (>24w)
    ]

    for offset_secs, expected_bucket in test_cases:
        state_dir = tmp_path / f"state_{expected_bucket}"
        state_dir.mkdir()
        (state_dir / "epoch").write_text(str(now - offset_secs))

        env = os.environ.copy()
        env["PATH"] = f"{mock_bin}:{env['PATH']}"
        env["STATE_DIRECTORY"] = str(state_dir)
        env["IMAGE_INFO"] = str(image_info)
        env["DISABLED_FILE"] = str(tmp_path / "nonexistent-disabled")

        res = subprocess.run([str(SCRIPT_PATH)], env=env, capture_output=True, text=True)
        assert res.returncode == 0
        args = curl_args_log.read_text()
        assert f"countme={expected_bucket}" in args
