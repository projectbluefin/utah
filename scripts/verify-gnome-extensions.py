#!/usr/bin/env python3
"""Verify Utah's bundled GNOME extensions are present and declare GNOME 51."""

from __future__ import annotations

import argparse
import json
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
    "search-light@icedman.github.com": "search-light@icedman.github.com/metadata.json",
}


def shell_versions(path: Path) -> list[str]:
    try:
        return json.loads(path.read_text().replace("@PACKAGE_VERSION@", "0"))["shell-version"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError(f"invalid metadata: {error}") from error


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
        if "51" not in versions:
            failures.append(f"{uuid}: GNOME 51 not declared ({versions})")

    if failures:
        print("GNOME extension contract failed:", *failures, sep="\n  ", file=sys.stderr)
        return 1
    print(f"GNOME extension contract passed: {len(SOURCE_EXTENSIONS)} extensions declare GNOME 51")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
