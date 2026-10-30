#!/usr/bin/env python3
"""Verify Utah's bundled GNOME extensions are present and declare GNOME 51."""

from __future__ import annotations

import argparse
import configparser
import os
import re
import subprocess
from datetime import datetime, timezone
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
}


EXTENSION_BASE = "system_files/shared/usr/share/gnome-shell/extensions/"
BLING_BASE = Path("tmp/bluefin-bling/extensions")


def bling_extensions(base: Path) -> dict[str, str]:
    """Discover every bling folder; UUIDs must be safe directory/table names."""
    folders = sorted(path for path in (base / BLING_BASE).glob("*/") if path.is_dir())
    if not folders:
        raise ValueError(f"no bluefin-bling extensions found under {base / BLING_BASE}")
    extensions = {}
    for folder in folders:
        metadata = folder / "metadata.json"
        try:
            data = json.loads(metadata.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid metadata {metadata}: {error}") from error
        uuid = data.get("uuid") if isinstance(data, dict) else None
        if not isinstance(uuid, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9_.-]+", uuid):
            raise ValueError(f"invalid uuid in {metadata}: {uuid!r}")
        relative = str(metadata.relative_to(base))
        if uuid in extensions or (uuid in SOURCE_EXTENSIONS and SOURCE_EXTENSIONS[uuid] != relative):
            raise ValueError(f"duplicate uuid in {metadata}: {uuid}")
        extensions[uuid] = relative
    return extensions


# The installed verifier has no submodule tree: tmp/ is removed by the build.
# There, the source-generated provenance supplies the complete expected set.
_SOURCE_BASE = Path(__file__).resolve().parents[1] / EXTENSION_BASE
if _SOURCE_BASE.is_dir():
    SOURCE_EXTENSIONS.update(bling_extensions(_SOURCE_BASE))

ARCHIVES = {
    "tilingshell@ferrarodomenico.com": "tilingshell",
    "quick-settings-audio-panel@rayzeq.github.io": "qsap",
    "quicksettings-audio-devices-hider@marcinjahn.com": "hider",
    "quicksettings-audio-devices-renamer@marcinjahn.com": "renamer",
}


def source_provenance() -> list[dict]:
    """Resolve every installed UUID to its checked-out, indexed gitlink."""
    modules = configparser.ConfigParser()
    modules.read(".gitmodules")
    paths = {modules[s]["path"]: modules[s]["url"] for s in modules.sections()}
    used = set()
    records = []
    build = Path("scripts/build-gnome-extensions.sh").read_text()
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
        record = {"uuid": uuid, "repo": paths[path], "commit": commit}
        if uuid in ARCHIVES:
            prefix = ARCHIVES[uuid]
            digest = re.search(rf'^{prefix}_sha256="([0-9a-f]{{64}})"$', build, re.M)
            url = re.search(rf'^curl -fsSL "([^"\n]+)" -o "\$\{{{prefix}_zip\}}"$', build, re.M)
            if not digest or not url:
                raise ValueError(f"{uuid}: cannot resolve verified archive")
            # The gitlink is reference source; the verified archive is what ships.
            record["source_commit"] = record.pop("commit")
            record["archive"] = {"url": url[1], "digest": "sha256:" + digest[1]}
        records.append(record)
    if used != set(paths):
        raise ValueError(f"unrecorded submodules: {set(paths) - used}")
    return records


def record_provenance(root: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", os.environ["SHA_HEAD"]):
        raise ValueError("SHA_HEAD must be a full Utah commit")
    records = json.loads(os.environ["GNOME_EXTENSION_PROVENANCE"])
    if sorted(r["uuid"] for r in records) != sorted(SOURCE_EXTENSIONS):
        raise ValueError("provenance UUID list differs from extension contract")
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
        "gnome_extensions": records,
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="store_true", help="check submodule sources")
    parser.add_argument("--root", type=Path, default=Path("/"), help="image filesystem root")
    parser.add_argument("--print-provenance", action="store_true", help="emit checked source pins for build arguments")
    parser.add_argument("--record-provenance", action="store_true", help="write image provenance from build arguments")
    args = parser.parse_args()
    if args.print_provenance:
        print(json.dumps(source_provenance(), separators=(",", ":")))
        return 0

    base = (
        Path("system_files/shared/usr/share/gnome-shell/extensions")
        if args.source
        else args.root / "usr/share/gnome-shell/extensions"
    )
    if not args.source and not _SOURCE_BASE.is_dir():
        provenance = args.root / "usr/share/utah/provenance.json"
        if "GNOME_EXTENSION_PROVENANCE" in os.environ:
            records = json.loads(os.environ["GNOME_EXTENSION_PROVENANCE"])
        else:
            records = json.loads(provenance.read_text())["gnome_extensions"]
        for record in records:
            uuid = record["uuid"]
            if not isinstance(uuid, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9_.-]+", uuid):
                raise ValueError(f"invalid provenance uuid: {uuid!r}")
            SOURCE_EXTENSIONS.setdefault(uuid, f"{uuid}/metadata.json")

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

    if args.source:
        source_provenance()
    if args.record_provenance:
        record_provenance(args.root)
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
