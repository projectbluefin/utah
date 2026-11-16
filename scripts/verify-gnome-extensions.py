#!/usr/bin/env python3
"""Verify Utah's GNOME extensions are present and declare GNOME 51."""

from __future__ import annotations

import argparse
import configparser
import os
import re
import subprocess
import tomllib
from datetime import datetime, timezone
import json
import stat
import sys
from pathlib import Path

# Extensions still built from a pinned submodule, by scripts/build-gnome-extensions.sh.
SOURCE_EXTENSIONS = {
    "gsconnect@andyholmes.github.io": "gsconnect@andyholmes.github.io/data/metadata.json.in",
}

# Dakota's other extensions (elements/bluefin/gnome-shell-extensions.bst), each
# installed from the factory RPM named here (#1235). packages/utah.toml
# [parity] requests the packages; provenance is the installed NEVRA.
PACKAGED_EXTENSIONS = {
    "appindicatorsupport@rgcjonas.gmail.com": "gnome-shell-extension-appindicator",
    "bazaar-integration@kolunmi.github.io": "gnome-shell-extension-bazaar-companion",
    "blur-my-shell@aunetx": "gnome-shell-extension-blur-my-shell",
    "BudsLink-Companion@maniacx.github.com": "gnome-shell-extension-budslink-companion",
    "caffeine@patapon.info": "gnome-shell-extension-caffeine",
    "custom-command-list@storageb.github.com": "gnome-shell-extension-custom-command-menu",
    "dash-to-dock@micxgx.gmail.com": "gnome-shell-extension-dash-to-dock",
    "fuzzy-application-search@mkhl.codeberg.page": "gnome-shell-extension-fuzzy-application-search",
    "gradia-integration@alexandervanhee.github.io": "gnome-shell-extension-gradia-capture",
    "power-status-color@projectbluefin.io": "gnome-shell-extension-power-status-color",
    "quicksettings-audio-devices-hider@marcinjahn.com": "gnome-shell-extension-quicksettings-audio-devices-hider",
    "quicksettings-audio-devices-renamer@marcinjahn.com": "gnome-shell-extension-quicksettings-audio-devices-renamer",
    "syncthing-toggle@projectbluefin.io": "gnome-shell-extension-syncthing-toggle",
    "tailscale-gnome-qs@tailscale-qs.github.io": "gnome-shell-extension-tailscale-qs",
    "tilingshell@ferrarodomenico.com": "gnome-shell-extension-tilingshell",
}

EXTENSIONS = sorted({*SOURCE_EXTENSIONS, *PACKAGED_EXTENSIONS})

EXTENSION_BASE = "system_files/shared/usr/share/gnome-shell/extensions/"
MANIFEST = Path("packages/utah.toml")
SCHEMA_PATH = re.compile(r'path="([^"]+)"')


def source_provenance() -> list[dict]:
    """Resolve every submodule extension to its checked-out, indexed gitlink."""
    modules = configparser.ConfigParser()
    modules.read(".gitmodules")
    paths = {modules[s]["path"]: modules[s]["url"] for s in modules.sections()}
    used = set()
    records = []
    for uuid, relative in sorted(SOURCE_EXTENSIONS.items()):
        metadata = EXTENSION_BASE + relative
        matches = [path for path in paths if metadata.startswith(path + "/")]
        if len(matches) != 1:
            raise ValueError(f"{uuid}: expected one submodule, found {matches}")
        path = matches[0]
        used.add(path)
        entry = subprocess.check_output(["git", "ls-files", "--stage", "--", path], text=True).split()
        if len(entry) < 3 or entry[0] != "160000" or entry[2] != "0":
            raise ValueError(f"{path}: not an unmerged-free gitlink")
        commit = subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True).strip()
        if commit != entry[1]:
            raise ValueError(f"{path}: checkout does not match gitlink")
        subprocess.run(["git", "-C", path, "diff", "--exit-code", "HEAD", "--"], check=True, stdout=subprocess.DEVNULL)
        records.append({"uuid": uuid, "repo": paths[path], "commit": commit})
    if used != set(paths):
        raise ValueError(f"unrecorded submodules: {set(paths) - used}")
    return records


def manifest_errors(manifest: Path = MANIFEST) -> list[str]:
    """Every packaged extension must be requested by the install manifest."""
    requested = set(tomllib.loads(manifest.read_text()).get("parity", {}).get("packages", []))
    return [
        f"{uuid}: {package} is not in {manifest} [parity]"
        for uuid, package in sorted(PACKAGED_EXTENSIONS.items())
        if package not in requested
    ]


def packaged_provenance(root: Path) -> list[dict]:
    """Record the installed package that owns each packaged extension."""
    records = []
    for uuid, package in sorted(PACKAGED_EXTENSIONS.items()):
        path = Path("/usr/share/gnome-shell/extensions") / uuid
        result = subprocess.run(
            ["rpm", "--root", str(root), "-qf", "--qf", "%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n", str(path)],
            capture_output=True, text=True, check=False,
        )
        owners = result.stdout.split("\n")[0].split() if result.returncode == 0 else []
        if len(owners) != 2 or owners[0] != package:
            raise ValueError(f"{uuid}: expected {package} to own {path}, rpm reports {result.stdout.strip() or result.stderr.strip()!r}")
        records.append({"uuid": uuid, "package": package, "nevra": f"{package}-{owners[1]}"})
    return records


def record_provenance(root: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", os.environ["SHA_HEAD"]):
        raise ValueError("SHA_HEAD must be a full Utah commit")
    records = json.loads(os.environ["GNOME_EXTENSION_PROVENANCE"])
    if sorted(r["uuid"] for r in records) != sorted(SOURCE_EXTENSIONS):
        raise ValueError("provenance UUID list differs from extension contract")
    records += packaged_provenance(root)
    for record in records:
        metadata = json.loads((root / "usr/share/gnome-shell/extensions" / record["uuid"] / "metadata.json").read_text())
        record["declared_version"] = metadata.get("version")
    def image(ref: str) -> dict:
        name, separator, digest = ref.partition("@")
        return {"ref": name, "digest": digest if separator else None}
    document = {
        "utah": {"commit": os.environ["SHA_HEAD"], "version": os.environ["VERSION"],
                 "built": datetime.fromtimestamp(int(os.environ["SOURCE_DATE_EPOCH"]), timezone.utc).isoformat()},
        "base_image": image(os.environ["BASE_IMAGE"]),
        "factory": image(os.environ["PACKAGE_IMAGE_REF"]),
        "common": image(os.environ["COMMON_IMAGE"] + "@" + os.environ["COMMON_IMAGE_SHA"]),
        "brew": image(os.environ["BREW_IMAGE"] + "@" + os.environ["BREW_IMAGE_SHA"]),
        "wallpapers": image(os.environ["WALLPAPERS_IMAGE"] + "@" + os.environ["WALLPAPERS_IMAGE_SHA"]),
        "gnome_extensions": sorted(records, key=lambda r: r["uuid"]),
    }
    destination = root / "usr/share/utah/provenance.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def shell_versions(path: Path) -> list[str]:
    try:
        return json.loads(path.read_text().replace("@PACKAGE_VERSION@", "0"))["shell-version"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError(f"invalid metadata: {error}") from error


def version_validation_disabled(source: bool, root: Path) -> bool:
    schema_override = (
        Path("system_files/shared/usr/share/glib-2.0/schemas/zz3-bluefin-unsupported-stuff.gschema.override")
        if source
        else root / "usr/share/glib-2.0/schemas/zz3-bluefin-unsupported-stuff.gschema.override"
    )
    if not schema_override.is_file():
        return False
    try:
        content = schema_override.read_text()
        return "disable-extension-version-validation=true" in content
    except OSError:
        return False


def dconf_section_errors(root: Path) -> list[str]:
    """Extension defaults in the distro dconf database must name a schema path.

    GSettings resolves a key by the schema's declared path=, not by the
    extension's UUID folder. A keyfile section no installed schema declares is
    ignored without a word, and the extension keeps its compiled default.
    """
    declared = set()
    for schema in (root / "usr/share/glib-2.0/schemas").glob("*.gschema.xml"):
        declared.update(path.strip("/") for path in SCHEMA_PATH.findall(schema.read_text(errors="replace")))
    errors = []
    for keyfile in sorted((root / "etc/dconf/db/distro.d").glob("*")):
        if not keyfile.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        try:
            parser.read_string(keyfile.read_text())
        except (configparser.Error, UnicodeError) as error:
            errors.append(f"{keyfile}: unreadable keyfile: {error}")
            continue
        for section in parser.sections():
            if section.startswith("org/gnome/shell/extensions/") and section not in declared:
                errors.append(f"{keyfile}: [{section}] matches no installed schema path; its keys are ignored")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="store_true", help="check submodule sources and the install manifest")
    parser.add_argument("--root", type=Path, default=Path("/"), help="image filesystem root")
    parser.add_argument("--print-provenance", action="store_true", help="emit checked source pins for build arguments")
    parser.add_argument("--record-provenance", action="store_true", help="write image provenance from build arguments")
    args = parser.parse_args()
    if args.print_provenance:
        print(json.dumps(source_provenance(), separators=(",", ":")))
        return 0

    validation_disabled = version_validation_disabled(args.source, args.root)
    failures: list[str] = []
    if args.source:
        base = Path(EXTENSION_BASE)
        checked = {uuid: base / relative for uuid, relative in SOURCE_EXTENSIONS.items()}
        failures += manifest_errors()
    else:
        base = args.root / "usr/share/gnome-shell/extensions"
        checked = {uuid: base / uuid / "metadata.json" for uuid in EXTENSIONS}
    for uuid, path in checked.items():
        if not path.is_file():
            failures.append(f"{uuid}: missing {path}")
            continue
        try:
            versions = shell_versions(path)
        except ValueError as error:
            failures.append(f"{uuid}: {error}")
            continue
        if "51" not in versions and not validation_disabled:
            failures.append(f"{uuid}: GNOME 51 not declared ({versions})")

    if args.source:
        source_provenance()
    if args.record_provenance:
        record_provenance(args.root)
    if not args.source:
        # Extension.getSettings() prefers an extension's own schemas/ over the
        # system directory, where common's overrides are compiled in, and the
        # image-wide compile cannot see a schema left there. Every extension
        # schema is installed globally; none may stay behind.
        for schemas in sorted(base.glob("*/schemas")):
            failures.append(f"{schemas}: extension-local schemas mask the global ones; install them in /usr/share/glib-2.0/schemas")
        failures += dconf_section_errors(args.root)
        # gnome-shell runs as the user: an unreadable file stops the extension
        # loading ("Permission denied"), and a writable one lets any user
        # change code every session runs. Symlinks carry no meaningful mode.
        for path in sorted(base.rglob("*")):
            if path.is_symlink():
                continue
            mode = path.lstat().st_mode
            if not mode & stat.S_IROTH:
                failures.append(f"{path}: not world-readable ({stat.filemode(mode)})")
            if stat.S_ISDIR(mode) and not mode & stat.S_IXOTH:
                failures.append(f"{path}: directory not traversable ({stat.filemode(mode)})")
            if mode & (stat.S_IWGRP | stat.S_IWOTH):
                failures.append(f"{path}: group/world-writable ({stat.filemode(mode)})")

    if failures:
        print("GNOME extension contract failed:", *failures, sep="\n  ", file=sys.stderr)
        return 1
    print(f"GNOME extension contract passed: {len(checked)} extensions declare GNOME 51")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
