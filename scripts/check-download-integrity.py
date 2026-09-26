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

# A verifier vouches for a download only from inside that download's scope:
# its own `&&` chain -- a backslash-continuation run is one command -- or the
# few lines that follow the fetch. A digest token anywhere else in the file
# says nothing about this fetch: file-wide clearance let one `sha256sum`
# vouch for every download added to the file later, in exactly the files
# that already do downloads.
SCOPE_LINES = 10

# Digest and signature evidence, matched on word boundaries. A bare `--check`
# is deliberately absent: as a substring it matched flags such as
# `--checkpoint`, and the real checks `sha256sum --check` and
# `sha512sum --check` are already covered by the digest tokens themselves.
VERIFIER_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bsha256sum\b",
        r"\bsha512sum\b",
        r"\bcosign\b",
        r"\bgpg --verify\b",
    )
)


def continuation_run(lines: list[str], index: int) -> tuple[int, int]:
    """First and last line of the backslash-continuation run holding index.

    A command split across `\\` continuations is one command, so its scope
    spans every line of the run, not just the line the fetch token is on.
    """
    start = index
    while start > 0 and lines[start - 1].rstrip().endswith("\\"):
        start -= 1
    end = index
    while end + 1 < len(lines) and lines[end].rstrip().endswith("\\"):
        end += 1
    return start, end


def command_chain(lines: list[str], index: int) -> tuple[int, int]:
    """Extent of the `&&` chain the command holding index runs in.

    The command is its continuation run; the chain grows through neighbouring
    commands that are `&&`-joined to it, in both directions. A comment line
    never joins: prose that ends in `&&` chains nothing.
    """
    start, end = continuation_run(lines, index)
    while start > 0:
        previous = lines[start - 1].strip()
        if previous.startswith("#") or not previous.rstrip("\\").strip().endswith("&&"):
            break
        start, _ = continuation_run(lines, start - 1)
    while end + 1 < len(lines):
        following = lines[end].strip()
        if following.startswith("#") or not following.rstrip("\\").strip().endswith("&&"):
            break
        _, end = continuation_run(lines, end + 1)
    return start, end


def verified_in_scope(lines: list[str], index: int) -> bool:
    """True when a verifier token appears inside the download's own scope.

    Scope is the fetch's `&&` chain, widened to the SCOPE_LINES lines that
    follow the fetch so a short verify helper after the download still
    counts. Comment lines are skipped: prose that mentions a digest tool is
    not a check.
    """
    start, end = command_chain(lines, index)
    end = min(len(lines) - 1, max(end, index + SCOPE_LINES))
    for position in range(start, end + 1):
        stripped = lines[position].strip()
        if stripped.startswith("#"):
            continue
        if any(pattern.search(stripped) for pattern in VERIFIER_PATTERNS):
            return True
    return False


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
        lines = text.splitlines()
        for number, stripped in logical_lines(text):
            if stripped.startswith("#"):
                continue
            if "releases/latest/download" in stripped:
                problems.append(f"{path}:{number}: resolves a mutable latest release")
            # A raw github release/CDN download of an executable asset must be
            # verified. Flag fetches of .run/.tar.gz/.tgz/.rpm/.flatpak and of
            # systemd units (.service/.timer run their payload as root) that
            # have no digest or signature check tied to the fetch: one in its
            # own command chain, or in the lines that follow it.
            if re.search(r"(curl|wget)\b", stripped) and not is_allowed(stripped):
                if re.search(r"\.(run|tar\.gz|tgz|rpm|flatpak|service|timer)\b", stripped):
                    if not verified_in_scope(lines, number - 1):
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
