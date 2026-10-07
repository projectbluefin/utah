#!/usr/bin/env python3
"""Assert review fixes on a disposable, booted Utah installation."""
import argparse
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


def verify_flavor(flavor, root=Path("/"), kernel=None):
    if not flavor:
        return {}
    metadata = json.loads((root / "usr/share/ublue-os/image-info.json").read_text())
    assert metadata["image-flavor"] == flavor, metadata
    kernel = kernel or require("uname", "-r")
    ogc = root / "usr/lib/utah/ogc-kernel-release"
    if "gaming" in flavor.split("-"):
        assert ogc.read_text().strip() == kernel, "installed system did not boot its OGC kernel"
    elif ogc.exists():
        assert ogc.read_text().strip() != kernel, "non-gaming flavor booted OGC"
    result = {"flavor": flavor, "kernel": kernel}
    if "nvidia" in flavor.split("-"):
        vermagic = require("modinfo", "-k", kernel, "-F", "vermagic", "nvidia")
        assert vermagic.split()[0] == kernel, "NVIDIA module does not match booted kernel"
        module = require("modinfo", "-k", kernel, "-n", "nvidia")
        assert Path(module).is_file(), module
        require("test", "-x", "/usr/bin/nvidia-smi")
        result["nvidia_module"] = module
        result["nvidia_vermagic"] = vermagic
        result["nvidia_hardware_test"] = "not available in QEMU"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flavor", default="")
    args = parser.parse_args()
    flavor_checks = verify_flavor(args.flavor)
    verifier = "/tmp/utah-review-rpm-contract.py"
    command = ["python3", verifier, "--no-report", "/usr/share/utah/bluefin.toml"]
    require(*command)
    timers = {unit: require("systemctl", "is-enabled", unit) for unit in (
        "bluefin-stats-refresh.timer", "projectbluefin-countme.timer")}
    optout = Path("/etc/projectbluefin/countme/disabled")
    assert not optout.exists(), "clean VM unexpectedly opted out"
    require("systemctl", "stop", "projectbluefin-countme.timer", "projectbluefin-countme.service")
    optout.parent.mkdir(parents=True, exist_ok=True)
    try:
        optout.touch()
        require("systemctl", "start", "projectbluefin-countme.service")
        condition = require("systemctl", "show", "projectbluefin-countme.service", "--property=ConditionResult", "--value")
        assert condition == "no", condition
    finally:
        optout.unlink(missing_ok=True)
        require("systemctl", "start", "projectbluefin-countme.timer")
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
                      "timers": timers, "countme_optout": "passed", "greeter_png": dimensions, "about_artwork": artwork,
                      "kernel": require("uname", "-r"), "flavor_checks": flavor_checks}))


if __name__ == "__main__":
    main()
