#!/usr/bin/env python3
"""Verify Utah's non-RPM desktop contract.

The checker runs inside a composed image for runtime verification, while
``--check`` validates the contract itself in source-only CI. RPM parity remains
in verify-rpm-contract.py; this covers Utah identity, Bluefin desktop defaults,
Flatpak policy, and required service enablement.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def read_os_release(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def verify_values(
    name: str, actual: dict[str, Any], expected: dict[str, str], patterns: dict[str, str]
) -> list[str]:
    errors: list[str] = []
    for key, value in expected.items():
        if actual.get(key) != value:
            errors.append(f"{name} {key} must be {value!r}, got {actual.get(key)!r}")
    for key, pattern in patterns.items():
        value = str(actual.get(key, ""))
        if not re.fullmatch(pattern, value):
            errors.append(f"{name} {key} must match {pattern!r}, got {value!r}")
    return errors


def parse_brewfile(path: Path) -> list[str]:
    app_pattern = re.compile(r'^flatpak\s+"([^"]+)"\s*$')
    apps: list[str] = []
    for line in path.read_text().splitlines():
        match = app_pattern.match(line.strip())
        if match:
            apps.append(match.group(1))
    return apps


def unit_enabled(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-enabled", unit], capture_output=True, text=True, check=False
    )
    return result.returncode == 0 and result.stdout.strip() in {"enabled", "enabled-runtime"}


def validate_contract(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for section in ("branding", "configuration", "flatpak", "services"):
        if section not in contract:
            errors.append(f"missing [{section}] section")
    for section in ("branding", "configuration", "flatpak"):
        for file_name in contract.get(section, {}).get("files", []):
            if not Path(file_name).is_absolute():
                errors.append(f"{section} file must be absolute: {file_name}")
    flatpak = contract.get("flatpak", {})
    apps = flatpak.get("apps", [])
    if not apps:
        errors.append("Flatpak app contract must not be empty")
    if len(apps) != len(set(apps)):
        errors.append("Flatpak app contract contains duplicate IDs")
    if not str(flatpak.get("brewfile", "")).startswith("/"):
        errors.append("Flatpak Brewfile path must be absolute")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate contract syntax only")
    parser.add_argument("contract", type=Path)
    args = parser.parse_args()

    contract = tomllib.loads(args.contract.read_text())
    errors = validate_contract(contract)
    if args.check:
        for error in errors:
            fail(error)
        if not errors:
            print("desktop contract syntax is valid")
        return int(bool(errors))

    branding = contract["branding"]
    configuration = contract["configuration"]
    flatpak = contract["flatpak"]
    services = contract["services"]

    for section in (branding, configuration, flatpak):
        for file_name in section.get("files", []):
            if not Path(file_name).is_file():
                errors.append(f"required file is missing: {file_name}")

    os_release = read_os_release(Path("/usr/lib/os-release"))
    errors.extend(
        verify_values(
            "os-release",
            os_release,
            branding.get("os_release", {}),
            branding.get("os_release_patterns", {}),
        )
    )

    image_info_path = Path("/usr/share/ublue-os/image-info.json")
    if image_info_path.is_file():
        try:
            image_info = json.loads(image_info_path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"invalid image-info.json: {exc}")
        else:
            errors.extend(
                verify_values(
                    "image-info",
                    image_info,
                    branding.get("image_info", {}),
                    branding.get("image_info_patterns", {}),
                )
            )

    for file_name, expected_lines in configuration.get("file_contains", {}).items():
        path = Path(file_name)
        if not path.is_file():
            continue
        content = path.read_text()
        for expected in expected_lines:
            if expected not in content:
                errors.append(f"{file_name} is missing required setting: {expected!r}")

    brewfile = Path(flatpak["brewfile"])
    if brewfile.is_file():
        actual_apps = parse_brewfile(brewfile)
        expected_apps = flatpak["apps"]
        if actual_apps != expected_apps:
            missing = [app for app in expected_apps if app not in actual_apps]
            extra = [app for app in actual_apps if app not in expected_apps]
            errors.append(
                "Flatpak Brewfile differs from the contract"
                f" (missing: {', '.join(missing) or 'none'}; extra: {', '.join(extra) or 'none'})"
            )

    remote = Path(flatpak["remote"])
    if remote.is_file() and f"Url={flatpak['remote_url']}" not in remote.read_text():
        errors.append(f"{remote} does not configure {flatpak['remote_url']}")

    for unit in services.get("enabled", []):
        if not unit_enabled(unit):
            errors.append(f"required service is not enabled: {unit}")

    for error in errors:
        fail(error)
    if errors:
        return 1
    print(
        f"Utah desktop contract passed: {len(flatpak['apps'])} Flatpaks, "
        f"{len(services.get('enabled', []))} enabled services"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
