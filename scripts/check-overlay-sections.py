#!/usr/bin/env python3
"""Every section of packages/utah.toml must be consumed by both scripts.

install-packages.py decides what the image installs; check-repo-availability.py
decides what gets preflighted. Both compose their list from sections named one
by one, so a section added to utah.toml and wired into only one of them is
silently half-live: installed but never verified, or verified but never
installed. Neither failure is loud.

That has happened twice. [services] was installed and never preflighted. Then
[firmware] was added -- for packages whose absence had already shipped an image
with no wifi -- and was, for one commit, installed and unverified in exactly
the same way. This turns the next occurrence into a failure at `just check`
rather than a discovery on hardware.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERLAY = ROOT / "packages" / "utah.toml"
# Each consumer, with the sections it is allowed not to read and why. An
# exemption is a claim about the section's nature, so it has to be written
# down; anything not exempted must be read.
CONSUMERS = {
    "install-packages.py": set(),
    "check-repo-availability.py": set(),
    # [build] is the toolchain for scripts/build-gnome-extensions.sh. It is
    # installed, used and removed inside the build, so it is deliberately not
    # part of what the finished image must contain.
    "verify-rpm-contract.py": {"build"},
}
# [unavailable] records contract packages no repository provides. Every script
# reads it to subtract rather than to add, so it is never in the wanted set.
EXEMPT = {"unavailable"}
SECTION = re.compile(r'section\((?:[A-Za-z_.\[\]"\' ]+),\s*"([a-z_]+)"\)')


def main() -> int:
    sections = set(tomllib.loads(OVERLAY.read_text())) - EXEMPT
    failed = False
    for name, exempt in CONSUMERS.items():
        consumer = ROOT / "scripts" / name
        read = set(SECTION.findall(consumer.read_text()))
        missing = sorted(sections - read - exempt)
        if missing:
            failed = True
            print(
                f"ERROR: scripts/{name} never reads "
                f"{', '.join('[' + section + ']' for section in missing)} "
                f"from packages/utah.toml",
                file=sys.stderr,
            )
    if failed:
        print(
            "Add the section to the list that script composes, or move its "
            "packages into a section that is already read.",
            file=sys.stderr,
        )
        return 1
    print(
        f"overlay sections read by every consumer that must: "
        f"{', '.join(sorted(sections))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
