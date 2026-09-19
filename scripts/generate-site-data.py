#!/usr/bin/env python3
"""Generate the status page's package data from the shipped manifests.

The page must not carry a hand-maintained package list: it would drift from
packages/bluefin.toml and packages/utah.toml the first time either changed,
and a status page that lies is worse than no status page. The grid is built
from the same functions install-packages.py uses to resolve the real install
set, so the counts on the page are the counts the image build asks for.

`just check` re-runs this with --check and fails if the committed JSON is
stale, so the two cannot diverge silently.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Overlay sections in the order the page shows them, with the description each
# group's card carries. Keep in step with packages/utah.toml's header.
GROUPS = [
    ("gnome", "GNOME 51 desktop",
     "The desktop contract Hummingbird does not ship. Built by the factory."),
    ("parity", "Base-image parity",
     "What Bluefin inherits from Fedora's base image and Hummingbird has in "
     "its repository but not in its bootable base."),
    ("hardware", "Firmware",
     "Device firmware the bootable base leaves out, so a driver that needs a "
     "blob can find one."),
    ("services", "Desktop services",
     "Units Bluefin adds on top of the server base; the preset cannot enable "
     "what was never installed."),
    ("build", "Extension toolchain",
     "Build-only dependencies for the pinned GNOME extensions. These are "
     "removed again before the image ships, so they are listed for "
     "completeness and are not counted as installed."),
]

# Sections whose packages the image builds with but does not ship.
# configure-services.sh removes them after the extensions are built, so
# counting them as installed would overstate what a user receives.
TRANSIENT = {"build"}


def _display(path: Path) -> str:
    """Path relative to the repository when it is inside it, else as given.

    --check is pointed at a temporary file by the tests, and relative_to()
    raises rather than falling back for a path outside the tree.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_installer():
    spec = importlib.util.spec_from_file_location(
        "install_packages", ROOT / "scripts" / "install-packages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unavailable_reasons(overlay: Path) -> dict[str, list[int]]:
    """Map each [unavailable] package to the issue numbers its comment cites.

    The manifest documents every gap in prose above the list, with a tracking
    issue, because the rule there is that an entry without one is not allowed.
    Surfacing the issue numbers is what turns the page's gap list from a wall
    of names into something a reader can follow.
    """
    text = overlay.read_text()
    # Split on the section header at the start of a line. Plain "[unavailable]"
    # also appears in the file's own header comment listing the sections, and
    # splitting on that parsed the wrong block and found no issues at all.
    _, _, after = text.partition("\n[unavailable]\n")
    block = after.split("packages = [", 1)[0]
    reasons: dict[str, list[int]] = {}
    current: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        body = stripped.lstrip("#").strip()
        if not body:
            current = []
            continue
        # A comment line naming only package names starts a new entry.
        if re.fullmatch(r"[a-z0-9][a-z0-9.+_-]*(,\s*[a-z0-9][a-z0-9.+_-]*)*", body):
            current = [name.strip() for name in body.split(",")]
            for name in current:
                reasons.setdefault(name, [])
        elif current:
            # Only the explicit "Tracked by" clause counts. The prose also
            # cites neighbouring issues to draw comparisons -- ppp's entry
            # names avahi's #104 and nautilus's #100 while being tracked by
            # #107 -- and scraping every "#123" in the paragraph attributed
            # all three to ppp. Cross-repository references carry their repo
            # (utah-packages#112) and are left out: these numbers are rendered
            # as links into this repository.
            for clause in re.findall(r"[Tt]racked by ([^.]*)", body):
                for number in re.findall(r"(?<![\w-])#(\d+)", clause):
                    for name in current:
                        if int(number) not in reasons[name]:
                            reasons[name].append(int(number))
    return reasons


def build(root: Path) -> dict:
    installer = load_installer()
    base = root / "packages" / "bluefin.toml"
    overlay = root / "packages" / "utah.toml"
    section = installer.section

    unavailable = section(overlay, "unavailable")
    reasons = unavailable_reasons(overlay)
    overlay_names: set[str] = set()
    groups = []
    for key, title, blurb in GROUPS:
        names = sorted(section(overlay, key))
        if key not in TRANSIENT:
            overlay_names.update(names)
        if names:
            groups.append({"id": key, "title": title, "blurb": blurb,
                           "transient": key in TRANSIENT, "packages": names})

    # Everything the Bluefin contract asks for that Utah does not override.
    contract = installer.contract(base, overlay, None)
    bluefin = sorted(name for name in contract if name not in overlay_names)
    groups.insert(0, {
        "id": "bluefin",
        "title": "Bluefin contract",
        "blurb": "Packages Utah installs because Bluefin's own manifest asks "
                 "for them. This file is a byte-for-byte copy of upstream's.",
        "transient": False,
        "packages": bluefin,
    })

    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "generator": "scripts/generate-site-data.py",
        "totals": {
            "installed": len(contract),
            "unavailable": len(unavailable),
        },
        "groups": groups,
        "unavailable": [
            {"name": name, "issues": reasons.get(name, [])} for name in sorted(unavailable)
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "site/data/packages.json")
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed file differs from a fresh build")
    args = parser.parse_args(argv)

    data = build(ROOT)
    rendered = json.dumps(data, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not args.output.is_file():
            print(f"error: {args.output} does not exist; run without --check", file=sys.stderr)
            return 1
        current = json.loads(args.output.read_text())
        fresh = json.loads(rendered)
        # generated_at moves every day and says nothing about the packages.
        current.pop("generated_at", None)
        fresh.pop("generated_at", None)
        if current != fresh:
            print(f"error: {args.output} is stale; regenerate with "
                  "python3 scripts/generate-site-data.py", file=sys.stderr)
            return 1
        print(f"{_display(args.output)} is current")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    print(f"wrote {_display(args.output)}: "
          f"{data['totals']['installed']} installed, "
          f"{data['totals']['unavailable']} documented gaps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
