#!/usr/bin/env python3
"""Generate/check the device catalog from canonical skills' on_device markers.

Build-time only: none of this generator is installed in the image.
"""
from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

from generate_skill_index import find_skill_files, parse_front_matter

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "docs/agent"


def catalog(sources: list[Path], bundle: Path = BUNDLE) -> dict:
    entries = []
    for source in sources:
        fm = parse_front_matter(source)
        if "on_device" not in fm:
            continue
        target = fm["on_device"]
        expected = f"docs/agent/skills/{fm['id']}.md"
        if target != expected:
            raise ValueError(f"{source}: on_device must be {expected}")
        relative = f"skills/{fm['id']}.md"
        if not (bundle / relative).is_file():
            raise ValueError(f"{source}: missing device edition {relative}")
        entries.append({"id": fm["id"], "entry_point": relative,
                        "description": fm["one_line_purpose"]})
    entries.sort(key=lambda entry: entry["id"])
    ids = [entry["id"] for entry in entries]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate device skill id")
    expected_files = {entry["entry_point"] for entry in entries}
    actual = {p.relative_to(bundle).as_posix()
              for p in (bundle / "skills").rglob("*") if p.is_file()}
    if expected_files != actual:
        raise ValueError(f"unmarked device skill files: {sorted(actual - expected_files)}")
    return {"schema_version": "1.0", "skills": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true")
    group.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        device_catalog = catalog(find_skill_files())
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        required = set(contract["branding"]["files"])
        for entry in device_catalog["skills"]:
            installed = f"/usr/share/utah/agent/{entry['entry_point']}"
            if installed not in required:
                raise ValueError(f"device skill missing from image contract: {installed}")
        text = json.dumps(device_catalog, indent=2) + "\n"
        path = BUNDLE / "index.json"
        if args.write:
            path.write_text(text)
        elif not path.exists() or path.read_text() != text:
            raise ValueError("stale device catalog; run python3 scripts/generate_agent_context.py --write")
    except ValueError as exc:
        parser.exit(1, f"error: {exc}\n")
    print("Device skill catalog is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
