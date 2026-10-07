#!/usr/bin/env python3
"""Verify that an installed ISO booted the requested flavor and kernel."""
import argparse
import json
from pathlib import Path
import subprocess


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
    parser.add_argument("--flavor", required=True)
    args = parser.parse_args()
    print(json.dumps(verify_flavor(args.flavor)))


if __name__ == "__main__":
    main()
