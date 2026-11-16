#!/usr/bin/env python3
"""Fail when the hand-written package counts drift from the manifests.

README.md's "Parity with Dakota" table and docs/skills/
package-contract.md's "Current counts" sentence quote the same three numbers
by hand: how many base manifest packages Utah installs as-is, how many it
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
    """(base manifest installed, Utah additions, genuinely unavailable)."""
    data = load_generator().build(ROOT)
    base = next(g for g in data["groups"] if g["id"] == "base")
    base_count = len(base["packages"])
    installed = data["totals"]["installed"]
    unavailable = data["totals"]["unavailable"]
    return base_count, installed - base_count, unavailable


def count_sites() -> list[tuple[Path, str, str, int]]:
    """(file, label, pattern, index into expected_counts()); group 1 is the
    number. Patterns use \\s+ between words because package-contract.md wraps
    the sentence across lines. Built per call so tests can repoint the paths."""
    return [
        (README, "README.md Base manifest installed",
         r"Base manifest installed \| \*\*(\d+)\*\*", 0),
        (README, "README.md Utah additions", r"Utah additions \([^)]*\) \| (\d+)", 1),
        (README, "README.md Genuinely unavailable",
         r"Genuinely unavailable \| \*\*(\d+)\*\*", 2),
        (PACKAGE_CONTRACT_SKILL, "package-contract.md base manifest installed",
         r"(\d+)\s+base\s+manifest\s+packages\s+installed", 0),
        (PACKAGE_CONTRACT_SKILL, "package-contract.md Utah additions",
         r"(\d+)\s+Utah\s+additions\s+\([^)]*\)", 1),
        (PACKAGE_CONTRACT_SKILL, "package-contract.md genuinely unavailable",
         r"(\d+)\s+genuinely\s+unavailable", 2),
    ]


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    write = argv == ["--write"]
    if argv and not write:
        print("usage: check-doc-counts.py [--write]", file=sys.stderr)
        return 2
    counts = expected_counts()
    base, additions, unavailable = counts

    sites = count_sites()
    texts = {path: path.read_text() for path in {site[0] for site in sites}}
    stale = {}
    for path, label, pattern, index in sites:
        text = texts[path]
        match = re.search(pattern, text)
        if not match:
            print(f"error: {path} is missing the expected package-count wording "
                  f"(pattern not found: {pattern!r})", file=sys.stderr)
            return 1
        actual, want = int(match.group(1)), counts[index]
        if actual != want:
            stale[label] = (actual, want)
            start, end = match.span(1)
            texts[path] = text[:start] + str(want) + text[end:]

    if stale and write:
        for path, text in texts.items():
            path.write_text(text)
        for label, (actual, want) in stale.items():
            print(f"updated {label}: {actual} -> {want}")
    elif stale:
        print("error: documented package counts are stale. Regenerate with "
              "`python3 scripts/generate-site-data.py` and "
              "`python3 scripts/check-doc-counts.py --write`:", file=sys.stderr)
        for label, (actual, want) in stale.items():
            print(f"  {label}: documented {actual}, manifests say {want}", file=sys.stderr)
        return 1

    print(f"documented package counts are current: {base} base manifest, "
          f"{additions} Utah additions, {unavailable} genuinely unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
