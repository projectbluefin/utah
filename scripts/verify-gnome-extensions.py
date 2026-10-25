#!/usr/bin/env python3
"""Verify Utah's bundled GNOME extensions are present and declare GNOME 51."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

SOURCE_EXTENSIONS = {
    "appindicatorsupport@rgcjonas.gmail.com": "appindicatorsupport@rgcjonas.gmail.com/metadata.json",
    "bazaar-integration@kolunmi.github.io": "tmp/bazaar-integration@kolunmi.github.io/src/metadata.json",
    "blur-my-shell@aunetx": "blur-my-shell@aunetx/metadata.json",
    "caffeine@patapon.info": "tmp/caffeine/caffeine@patapon.info/metadata.json",
    "custom-command-list@storageb.github.com": "custom-command-list@storageb.github.com/metadata.json",
    "dash-to-dock@micxgx.gmail.com": "dash-to-dock@micxgx.gmail.com/metadata.json",
    "gradia-integration@alexandervanhee.github.io": "gradia-integration@alexandervanhee.github.io/src/metadata.json",
    "gsconnect@andyholmes.github.io": "gsconnect@andyholmes.github.io/data/metadata.json.in",
    "tilingshell@ferrarodomenico.com": "tmp/tilingshell@ferrarodomenico.com/resources/metadata.json",
    "BudsLink-Companion@maniacx.github.com": "BudsLink-Companion@maniacx.github.com/metadata.json",
    "fuzzy-application-search@mkhl.codeberg.page": "fuzzy-application-search@mkhl.codeberg.page/metadata.json",
    "quick-settings-audio-panel@rayzeq.github.io": "tmp/quick-settings-audio-panel/resources/metadata.json",
    "quicksettings-audio-devices-hider@marcinjahn.com": "tmp/quicksettings-audio-devices-hider/resources/metadata.json",
    "quicksettings-audio-devices-renamer@marcinjahn.com": "tmp/quicksettings-audio-devices-renamer/resources/metadata.json",
    "tailscale-gnome-qs@tailscale-qs.github.io": "tmp/tailscale-gnome-qs/tailscale-gnome-qs@tailscale-qs.github.io/metadata.json",
    "syncthing-toggle@projectbluefin.io": "tmp/bluefin-bling/extensions/syncthing-toggle/metadata.json",
    "power-status-color@projectbluefin.io": "tmp/bluefin-bling/extensions/power-status-color/metadata.json",
}


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="store_true", help="check submodule sources")
    parser.add_argument("--root", type=Path, default=Path("/"), help="image filesystem root")
    args = parser.parse_args()

    base = (
        Path("system_files/shared/usr/share/gnome-shell/extensions")
        if args.source
        else args.root / "usr/share/gnome-shell/extensions"
    )
    validation_disabled = version_validation_disabled(args.source, args.root)
    failures: list[str] = []
    for uuid, relative in SOURCE_EXTENSIONS.items():
        path = base / (relative if args.source else f"{uuid}/metadata.json")
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

    if not args.source:
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
    print(f"GNOME extension contract passed: {len(SOURCE_EXTENSIONS)} extensions declare GNOME 51")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
