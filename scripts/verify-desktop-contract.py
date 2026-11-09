#!/usr/bin/env python3
"""Verify Utah's non-RPM desktop contract.

The checker runs inside a composed image for runtime verification, while
``--check`` validates the contract itself in source-only CI. RPM parity remains
in verify-rpm-contract.py; this covers Utah identity, Bluefin desktop defaults,
GSettings overrides, Flatpak policy, and required service enablement.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ElementTree
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
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", unit], capture_output=True, text=True, check=False
        )
        return result.returncode == 0 and result.stdout.strip() in {"enabled", "enabled-runtime"}
    except FileNotFoundError:
        return False


def user_unit_enabled(unit: str) -> bool:
    """Enabled for every user (systemctl --global), where desktop audio lives."""
    try:
        result = subprocess.run(
            ["systemctl", "--global", "is-enabled", unit], capture_output=True, text=True, check=False
        )
        return result.returncode == 0 and result.stdout.strip() in {"enabled", "enabled-runtime"}
    except FileNotFoundError:
        return False


def unit_masked(unit: str, root: Path = Path("/")) -> bool:
    usr_lib_path = root / "usr/lib/systemd/system" / unit
    if usr_lib_path.is_symlink() and usr_lib_path.resolve() == Path("/dev/null"):
        return True
    etc_path = root / "etc/systemd/system" / unit
    if etc_path.is_symlink() and etc_path.resolve() == Path("/dev/null"):
        return True
    if root == Path("/"):
        try:
            result = subprocess.run(
                ["systemctl", "is-enabled", unit], capture_output=True, text=True, check=False
            )
            return result.stdout.strip() == "masked"
        except FileNotFoundError:
            return False
    return False


def unit_disabled(unit: str, root: Path = Path("/")) -> bool:
    """True when a unit is deliberately kept off: masked, disabled, static or absent.

    This is the negative half of the service contract: the enabled/user_enabled
    and masked lists assert what must be on, this asserts what must stay off. A
    unit that is merely absent (its package not installed) is also off, so it
    counts here. A static unit has no [Install] section, so it cannot be enabled
    at all (ipp-usb.service is started only by udev on device hotplug).
    """
    if unit_masked(unit, root):
        return True
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", unit], capture_output=True, text=True, check=False
        )
        return result.stdout.strip() in {"disabled", "static", "not-found"}
    except FileNotFoundError:
        return False


def decoder_load_error(element: str) -> str | None:
    """Return why a GStreamer element does not load, or None when it does.

    Fails closed: a missing gst-inspect-1.0 is an error, not a pass.
    """
    try:
        probe = subprocess.run(
            ["gst-inspect-1.0", element], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return f"media decoder {element!r} cannot be probed: gst-inspect-1.0 is missing"
    if probe.returncode != 0:
        return f"media decoder {element!r} failed to load: {probe.stderr.strip()}"
    return None


OVERRIDE_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
GSETTINGS_VALUE_KEY = re.compile(r"^[A-Za-z0-9_.-]+ [A-Za-z0-9-]+$")


def override_section_errors(schema_dir: Path, allowed: dict[str, str]) -> tuple[list[str], int]:
    """Check that every override section in schema_dir names an installed schema.

    glib-compile-schemas drops an override section whose schema no
    *.gschema.xml in the directory declares, and ignores a second file that
    declares an existing schema id, both without a message or a non-zero exit.
    `allowed` maps schema ids that may stay unresolved to the reason; an entry
    that resolves, or that no override names, is stale and also an error.
    Returns the errors and the number of override sections that resolved.
    """
    errors: list[str] = []
    declared: dict[str, list[str]] = {}
    for schema_file in sorted(schema_dir.glob("*.gschema.xml")):
        try:
            schemalist = ElementTree.parse(schema_file).getroot()
        except (OSError, ElementTree.ParseError) as exc:
            errors.append(f"unreadable GSettings schema {schema_file}: {exc}")
            continue
        for schema in schemalist.iter("schema"):
            if schema.get("id"):
                declared.setdefault(schema.get("id"), []).append(schema_file.name)
    for schema_id, files in sorted(declared.items()):
        if len(files) > 1:
            errors.append(
                f"GSettings schema {schema_id} is declared by {', '.join(files)};"
                " glib-compile-schemas keeps one and silently ignores the rest"
            )

    overridden: dict[str, list[str]] = {}
    for override in sorted(schema_dir.glob("*.gschema.override")):
        for line in override.read_text().splitlines():
            match = OVERRIDE_SECTION.match(line)
            if match:
                # [schema:DESKTOP] limits a section to one desktop.
                schema_id = match.group(1).split(":", 1)[0].strip()
                overridden.setdefault(schema_id, []).append(override.name)

    resolved = 0
    for schema_id, files in sorted(overridden.items()):
        if schema_id in declared:
            resolved += len(files)
        elif schema_id not in allowed:
            errors.append(
                f"{', '.join(files)} overrides {schema_id}, but no installed schema declares it;"
                " glib-compile-schemas silently drops the section"
            )
    for schema_id in sorted(allowed):
        if schema_id in declared:
            errors.append(f"allowed unresolved override {schema_id} is installed now; remove it from the allowlist")
        elif schema_id not in overridden:
            errors.append(f"allowed unresolved override {schema_id} is not overridden any more; remove it from the allowlist")
    return errors, resolved


def gsettings_value_error(schema: str, key: str, expected: str) -> str | None:
    """Return why `gsettings get schema key` does not print expected, or None.

    Fails closed: a missing gsettings is an error, not a pass.
    """
    try:
        probe = subprocess.run(["gsettings", "get", schema, key], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return f"GSettings {schema} {key} cannot be read: gsettings is missing"
    if probe.returncode != 0:
        return f"GSettings {schema} {key} cannot be read: {probe.stderr.strip()}"
    actual = probe.stdout.strip()
    if actual != expected:
        return f"GSettings {schema} {key} is {actual}, expected {expected}"
    return None


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
    gsettings = contract.get("gsettings")
    if gsettings is not None:
        if not str(gsettings.get("schema_dir", "")).startswith("/"):
            errors.append("GSettings schema_dir must be absolute")
        for schema_id, reason in gsettings.get("unresolved_overrides", {}).items():
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"allowed unresolved override {schema_id} needs a reason")
        for name, value in gsettings.get("values", {}).items():
            if not GSETTINGS_VALUE_KEY.match(name) or not isinstance(value, str):
                errors.append(f"GSettings value must be 'schema key' = \"value\": {name!r}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="validate contract syntax only")
    modes.add_argument("--flatpaks", type=Path, metavar="BREWFILE", help="print declared Flatpak IDs")
    parser.add_argument("contract", type=Path, nargs="?")
    args = parser.parse_args()
    if args.flatpaks is not None:
        if args.contract is not None:
            parser.error("--flatpaks does not take a contract argument")
        try:
            apps = parse_brewfile(args.flatpaks)
        except OSError as exc:
            fail(str(exc))
            return 1
        for app in apps:
            print(app)
        return 0
    if args.contract is None:
        parser.error("contract is required unless --flatpaks is given")

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

    # /usr/lib/os-release is the canonical file; /etc/os-release is what the
    # GNOME About panel reads. On bases where it is a regular file rather than a
    # symlink, it can keep the base identity, so both must satisfy the contract.
    for os_release_path in ("/usr/lib/os-release", "/etc/os-release"):
        if not Path(os_release_path).is_file():
            errors.append(f"required file is missing: {os_release_path}")
            continue
        errors.extend(
            verify_values(
                os_release_path,
                read_os_release(Path(os_release_path)),
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

    for unit in services.get("user_enabled", []):
        if not user_unit_enabled(unit):
            errors.append(f"required user service is not enabled globally: {unit}")

    for unit in services.get("masked", []):
        if not unit_masked(unit):
            errors.append(f"required service is not masked: {unit}")

    for unit in services.get("disabled", []):
        if not unit_disabled(unit):
            errors.append(f"service must be disabled: {unit}")

    decoders = contract.get("media", {}).get("decoders", [])
    for element in decoders:
        error = decoder_load_error(element)
        if error:
            errors.append(error)

    # Malformed [gsettings] entries are already in errors from validate_contract.
    gsettings = contract.get("gsettings")
    resolved_overrides = 0
    gsettings_values = {}
    if gsettings is not None:
        schema_dir = Path(str(gsettings.get("schema_dir", "")))
        if schema_dir.is_absolute() and schema_dir.is_dir():
            override_errors, resolved_overrides = override_section_errors(
                schema_dir, gsettings.get("unresolved_overrides", {})
            )
            errors.extend(override_errors)
        elif schema_dir.is_absolute():
            errors.append(f"GSettings schema directory is missing: {schema_dir}")
        gsettings_values = gsettings.get("values", {})
        for name, expected in gsettings_values.items():
            if not GSETTINGS_VALUE_KEY.match(name) or not isinstance(expected, str):
                continue
            schema, key = name.split(" ", 1)
            error = gsettings_value_error(schema, key, expected)
            if error:
                errors.append(error)

    for error in errors:
        fail(error)
    if errors:
        return 1
    masked_count = len(services.get("masked", []))
    masked_msg = f", {masked_count} masked services" if masked_count else ""
    disabled_count = len(services.get("disabled", []))
    disabled_msg = f", {disabled_count} disabled services" if disabled_count else ""
    decoder_msg = f", {len(decoders)} media decoders" if decoders else ""
    gsettings_msg = (
        f", {resolved_overrides} GSettings override sections, {len(gsettings_values)} GSettings values"
        if gsettings is not None
        else ""
    )
    print(
        f"Utah desktop contract passed: {len(flatpak['apps'])} Flatpaks, "
        f"{len(services.get('enabled', []))} enabled services, "
        f"{len(services.get('user_enabled', []))} user services{masked_msg}{disabled_msg}{decoder_msg}"
        f"{gsettings_msg}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
