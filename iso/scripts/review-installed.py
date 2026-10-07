#!/usr/bin/env python3
"""Assert review fixes on a disposable, booted Utah installation."""
import json
from pathlib import Path
import struct
import subprocess
import tempfile


def run(*args):
    return subprocess.run(args, text=True, capture_output=True)


def require(*args):
    result = run(*args)
    if result.returncode:
        raise RuntimeError(f"{args}: {result.stdout}{result.stderr}")
    return result.stdout.strip()


def main():
    verifier = "/tmp/utah-review-rpm-contract.py"
    command = ["python3", verifier, "--no-report", "/usr/share/utah/bluefin.toml"]
    require(*command)
    timers = {unit: require("systemctl", "is-enabled", unit) for unit in (
        "bluefin-stats-refresh.timer", "projectbluefin-countme.timer")}
    logo = Path("/usr/share/pixmaps/bluefin-gdm-logo.png").read_bytes()
    assert logo[:8] == b"\x89PNG\r\n\x1a\n", "invalid greeter PNG"
    dimensions = struct.unpack(">II", logo[16:24])
    assert dimensions == (150, 64), dimensions
    artwork = {}
    for name in ("fedora_logo_med.png", "fedora_whitelogo_med.png", "fedora-logo.png", "system-logo-white.png"):
        path = Path("/usr/share/pixmaps") / name
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", path
        artwork[name] = struct.unpack(">II", data[16:24])
    assert Path("/usr/share/icons/hicolor/scalable/actions/ublue-logo-symbolic.svg").stat().st_size
    assert Path("/usr/share/icons/hicolor/icon-theme.cache").stat().st_size
    directory = Path("/etc/dnf/repos.override.d")
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, prefix="utah-review-", suffix=".repo") as override:
        for section, options in (
            ("public-hummingbird-x86_64-rpms", "gpgkey=https://invalid.example/key"),
            ("public-hummingbird-x86_64-rpms", "enabled=0\ngpgkey=https://invalid.example/key"),
            ("*", "gpgkey=https://invalid.example/key"),
            ("public-hummingbird-x86_64-rpms", "gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release-2/"),
        ):
            override.seek(0)
            override.truncate()
            override.write(f"[{section}]\n{options}\n")
            override.flush()
            result = run(*command)
            assert result.returncode and "gpgkey" in result.stderr, result
    require(*command)
    print(json.dumps({"rpm_contract": "passed", "gpgkey_override_rejection": "passed",
                      "timers": timers, "greeter_png": dimensions, "about_artwork": artwork,
                      "kernel": require("uname", "-r")}))


if __name__ == "__main__":
    main()
