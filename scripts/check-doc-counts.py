#!/usr/bin/env python3
"""Fail when the hand-written package counts drift from the manifests.

README.md's "Package parity with Bluefin" table and docs/skills/
package-contract.md's "Current counts" sentence quote the same three numbers
by hand: how many Bluefin contract packages Utah installs as-is, how many it
adds or supplies itself, and how many are genuinely unavailable. Those
numbers already drifted once -- packages/utah.toml's [unavailable] list grew
from 4 to 9 entries while both documents kept quoting 4 -- so this recomputes
them from generate-site-data.py's own build() (the same function that writes
site/data/packages.json) and fails loudly instead of letting prose lie about
the manifests again.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
PACKAGE_CONTRACT_SKILL = ROOT / "docs/skills/package-contract.md"


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_site_data", ROOT / "scripts" / "generate-site-data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_counts() -> tuple[int, int, int]:
    """(Bluefin contract installed, Utah additions, genuinely unavailable)."""
    data = load_generator().build(ROOT)
    bluefin = next(g for g in data["groups"] if g["id"] == "bluefin")
    bluefin_count = len(bluefin["packages"])
    installed = data["totals"]["installed"]
    unavailable = data["totals"]["unavailable"]
    return bluefin_count, installed - bluefin_count, unavailable


def find(pattern: str, text: str, path: Path) -> int:
    match = re.search(pattern, text)
    if not match:
        print(f"error: {path} is missing the expected package-count wording "
              f"(pattern not found: {pattern!r})", file=sys.stderr)
        raise SystemExit(1)
    return int(match.group(1))


def main() -> int:
    bluefin, additions, unavailable = expected_counts()

    readme = README.read_text()
    readme_bluefin = find(r"Bluefin contract installed \| \*\*(\d+)\*\*", readme, README)
    readme_additions = find(r"Utah additions \([^)]*\) \| (\d+)", readme, README)
    readme_unavailable = find(r"Genuinely unavailable \| \*\*(\d+)\*\*", readme, README)

    skill = re.sub(r"\s+", " ", PACKAGE_CONTRACT_SKILL.read_text())
    skill_bluefin = find(r"(\d+) Bluefin contract packages installed", skill,
                          PACKAGE_CONTRACT_SKILL)
    skill_additions = find(r"(\d+) Utah additions \([^)]*\)", skill, PACKAGE_CONTRACT_SKILL)
    skill_unavailable = find(r"(\d+) genuinely unavailable", skill, PACKAGE_CONTRACT_SKILL)
    found = {
        "README.md Bluefin contract installed": (readme_bluefin, bluefin),
        "README.md Utah additions": (readme_additions, additions),
        "README.md Genuinely unavailable": (readme_unavailable, unavailable),
        "package-contract.md Bluefin contract installed": (skill_bluefin, bluefin),
        "package-contract.md Utah additions": (skill_additions, additions),
        "package-contract.md genuinely unavailable": (skill_unavailable, unavailable),
    }

    stale = {label: (actual, want) for label, (actual, want) in found.items() if actual != want}
    if stale:
        print("error: documented package counts are stale. Regenerate with "
              "`python3 scripts/generate-site-data.py` and update the prose "
              "to match:", file=sys.stderr)
        for label, (actual, want) in stale.items():
            print(f"  {label}: documented {actual}, manifests say {want}", file=sys.stderr)
        return 1

    print(f"documented package counts are current: {bluefin} Bluefin contract, "
          f"{additions} Utah additions, {unavailable} genuinely unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
