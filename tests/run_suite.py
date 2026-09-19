#!/usr/bin/env python3
"""Run every host-side unit test in this tree, including nested directories.

`just test` used to be a single `python3 -m unittest discover -s tests`.
`unittest discover` only descends into a subdirectory it can import as a
package, and neither `tests/__init__.py` nor `tests/unit/__init__.py` exists,
so any suite placed in a subdirectory of `tests/` was skipped silently --
discovery reported `OK` whether the nested tests passed, failed, or were never
run at all. A suite that cannot fail is worse than no suite: it reads as
coverage in review while protecting nothing.

This module derives the set of directories to run instead of naming them.
Every directory under the root that contains a `test_*.py` file is discovered
separately, with that directory as its own top-level import directory, so a new
suite is covered the day it lands without adding `__init__.py` scaffolding or
editing any list.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PATTERN = "test_*.py"


def discover_test_dirs(root: Path) -> list[Path]:
    """Every directory at or below `root` holding at least one test module."""
    dirs = {path.parent for path in root.rglob(PATTERN) if path.is_file()}
    return sorted(dirs)


def run(root: Path, python: str = sys.executable) -> int:
    """Run each discovered directory; return the first non-zero exit code."""
    test_dirs = discover_test_dirs(root)
    if not test_dirs:
        print(f"error: no {PATTERN} files found under {root}", file=sys.stderr)
        return 1

    failed = 0
    for test_dir in test_dirs:
        print(f"==> {test_dir}", flush=True)
        result = subprocess.run(
            [python, "-m", "unittest", "discover",
             "-s", str(test_dir), "-t", str(test_dir), "-p", PATTERN],
            check=False,
        )
        if result.returncode != 0 and failed == 0:
            failed = result.returncode
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        default=str(Path(__file__).resolve().parent),
        help="directory to search for test modules (default: this directory)",
    )
    args = parser.parse_args(argv)
    return run(Path(args.root).resolve())


if __name__ == "__main__":
    sys.exit(main())
