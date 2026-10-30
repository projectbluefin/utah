#!/usr/bin/env python3
"""Print shell-version metadata safely for the GNOME extension bump report."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS_BASE = REPO_ROOT / "system_files/shared/usr/share/gnome-shell/extensions"

# Reuse the verifier's metadata map so the report reads the same files the
# source contract checks (many extensions keep metadata under src/ etc.).
_SPEC = importlib.util.spec_from_file_location(
    "verify_gnome_extensions", REPO_ROOT / "scripts/verify-gnome-extensions.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_VERIFIER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VERIFIER)
SOURCE_EXTENSIONS: dict[str, str] = _VERIFIER.SOURCE_EXTENSIONS

FALLBACK_LOCATIONS = ("metadata.json", "data/metadata.json.in")


def metadata_files(extension: Path, base: Path = EXTENSIONS_BASE) -> list[tuple[str, Path]]:
    """Return (uuid, metadata path) pairs for the extension submodule."""
    try:
        submodule = extension.resolve().relative_to(base.resolve())
    except ValueError:
        submodule = None
    if submodule == Path("tmp/bluefin-bling"):
        return [(uuid, base / relative) for uuid, relative in _VERIFIER.bling_extensions(base).items()]
    found = []
    if submodule is not None:
        for uuid, relative in SOURCE_EXTENSIONS.items():
            rel = Path(relative)
            if rel.parts[: len(submodule.parts)] == submodule.parts:
                found.append((uuid, extension / rel.relative_to(submodule)))
    if found:
        return found
    for location in FALLBACK_LOCATIONS:
        if (extension / location).is_file():
            return [("", extension / location)]
    return []


def read_versions(metadata: Path) -> str:
    try:
        data = json.loads(metadata.read_text().replace("@PACKAGE_VERSION@", "0"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read metadata {metadata}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"metadata {metadata} must be a JSON object")

    versions = data.get("shell-version")
    if not isinstance(versions, list) or not versions or not all(
        isinstance(item, str) and re.fullmatch(r"\d+(?:\.\d+)*", item)
        for item in versions
    ):
        raise ValueError(f"metadata {metadata} has invalid shell-version values")

    # Shell API versions are numeric. Reject anything else because this value
    # is rendered into a Markdown table in a write-enabled automation job.
    return ", ".join(versions)


def shell_versions(extension: Path, base: Path = EXTENSIONS_BASE) -> str:
    """Read the extension's metadata and escape values for a Markdown table."""
    files = metadata_files(extension, base)
    if not files:
        raise ValueError(f"no metadata found for extension {extension}")
    if len(files) == 1:
        return read_versions(files[0][1])
    # Discovered upstream UUIDs are validated before rendering Markdown.
    return "; ".join(f"{uuid}: {read_versions(path)}" for uuid, path in files)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: gnome-extension-shell-versions.py EXTENSION_PATH", file=sys.stderr)
        return 2
    try:
        versions = shell_versions(Path(sys.argv[1]))
    except ValueError as exc:
        print(f"Cannot propose extension bump: {exc}", file=sys.stderr)
        return 1
    print(versions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
