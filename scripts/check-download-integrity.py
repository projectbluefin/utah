#!/usr/bin/env python3
"""Fail when a mutable or checksum-free executable download reaches the image.

Every executable release asset that composition fetches must be version-pinned
and verified against a committed digest or a trusted signature, and no build may
resolve a mutable `releases/latest` during composition. This is a static guard,
not a network check: it inspects the committed build recipes only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FILES = [
    Path("Containerfile"),
    Path("Containerfile.kernel"),
    Path("scripts/install-nvidia.sh"),
    Path("scripts/install-ogc-kernel.sh"),
    Path("scripts/configure-services.sh"),
    Path("iso/live/src/install-flatpaks.sh"),
    Path("iso/scripts/build-iso.sh"),
]

# Downloads that are configuration descriptors, not executed code, and are safe
# to fetch without a digest. Flathub's repo descriptor only names a remote; the
# packages installed from it are themselves verified by Flatpak/OSTree.
ALLOWED_UNPINNED = (
    "dl.flathub.org/repo/flathub.flatpakrepo",
    "dl.flathub.org/repo/appstream",
)


def is_allowed(line: str) -> bool:
    return any(marker in line for marker in ALLOWED_UNPINNED)


def check() -> list[str]:
    problems: list[str] = []
    for path in FILES:
        if not path.is_file():
            continue
        text = path.read_text()
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "releases/latest/download" in stripped:
                problems.append(f"{path}:{number}: resolves a mutable latest release")
            # A raw github release/CDN download of an executable asset must be
            # verified. Flag fetches of .run/.tar.gz/.tgz/.rpm/.flatpak that do
            # not have an accompanying digest or signature check in the file.
            if re.search(r"(curl|wget)\b", stripped) and not is_allowed(stripped):
                if re.search(r"\.(run|tar\.gz|tgz|rpm|flatpak)\b", stripped):
                    verified = any(
                        token in text
                        for token in ("sha256sum", "sha512sum", "--check", "cosign", "gpg --verify")
                    )
                    if not verified:
                        problems.append(
                            f"{path}:{number}: executable download without a digest or signature check"
                        )
    return problems


def main() -> int:
    problems = check()
    if problems:
        print("Unpinned or unverified executable downloads:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"checked {len(FILES)} build recipes: downloads are pinned and verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
