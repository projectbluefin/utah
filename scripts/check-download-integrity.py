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


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations so a download is inspected as one command.

    `curl` and `wget` invocations in Containerfiles and shell scripts routinely
    carry their URL on a continuation line. Matching raw lines therefore sees a
    bare `curl -fsSL \\` with no URL and no asset suffix, and the download slips
    through unexamined. Each joined line is reported at the line number it
    starts on, which is where a reader looks for the command.
    """
    joined: list[tuple[int, str]] = []
    start = 0
    parts: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            if parts:
                # A comment inside a continuation run. The Dockerfile parser
                # drops such lines and the RUN keeps going, so skip it and let
                # the run continue; its trailing backslash decides nothing.
                continue
            # A comment on its own never starts a run: a trailing backslash in
            # a shell comment ends at the newline, so the next line is a
            # separate command and must be inspected on its own rather than
            # swallowed into an exempt comment.
            joined.append((number, stripped))
            continue
        if not parts:
            start = number
        if stripped.endswith("\\"):
            parts.append(stripped[:-1].strip())
            continue
        parts.append(stripped)
        joined.append((start, " ".join(part for part in parts if part)))
        parts = []
    if parts:
        joined.append((start, " ".join(part for part in parts if part)))
    return joined


def check() -> list[str]:
    problems: list[str] = []
    for path in FILES:
        if not path.is_file():
            continue
        text = path.read_text()
        for number, stripped in logical_lines(text):
            if stripped.startswith("#"):
                continue
            if "releases/latest/download" in stripped:
                problems.append(f"{path}:{number}: resolves a mutable latest release")
            # A raw github release/CDN download of an executable asset must be
            # verified. Flag fetches of .run/.tar.gz/.tgz/.rpm/.flatpak and of
            # systemd units (.service/.timer run their payload as root) that do
            # not have an accompanying digest or signature check in the file.
            if re.search(r"(curl|wget)\b", stripped) and not is_allowed(stripped):
                if re.search(r"\.(run|tar\.gz|tgz|rpm|flatpak|service|timer)\b", stripped):
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
