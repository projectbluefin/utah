#!/usr/bin/env python3
"""Syntax-check every script this repository tracks.

`just check` is the only source-level gate CI runs before a build starts
(.github/workflows/build.yml). It used to syntax-check scripts by naming them
one at a time, so the set of checked scripts was a hand-maintained list that
nothing compared against the set of scripts that actually exist. Six scripts
had fallen out of that list, including `scripts/resolve-e2e-inputs.py`, which
computes the E2E matrix, and `iso/scripts/live-kernel.py`, which runs in the
middle of an ISO build. A syntax error in any of them surfaced as a failure
minutes into a build or an E2E lane instead of in the gate.

This module owns that inventory by deriving it: every tracked file that is a
shell or Python script -- by extension, or by shebang for extensionless
executables -- is checked. A script added tomorrow is covered without editing
anything.
"""

from __future__ import annotations

import argparse
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path

SHELL_SHEBANGS = ("sh", "bash", "dash", "zsh")
PYTHON_SHEBANGS = ("python", "python3")


def tracked_files(root: Path) -> list[Path]:
    """Every file git tracks, so untracked scratch files are never checked."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [root / name for name in out.split("\0") if name]


def _shebang_interpreter(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            first = handle.readline(256)
    except OSError:
        return ""
    if not first.startswith(b"#!"):
        return ""
    line = first[2:].decode("utf-8", "replace").strip()
    words = line.split()
    if not words:
        return ""
    interpreter = Path(words[0]).name
    if interpreter == "env" and len(words) > 1:
        interpreter = Path(words[1]).name
    return interpreter


def classify(path: Path) -> str | None:
    """Return "shell", "python", or None for a file that is neither."""
    suffix = path.suffix
    if suffix == ".sh":
        return "shell"
    if suffix == ".py":
        return "python"
    if suffix:
        # A suffixed file that is not .sh/.py is not a script we own; a
        # shebang inside e.g. a .service or .conf file is data, not code.
        return None
    interpreter = _shebang_interpreter(path)
    if interpreter in SHELL_SHEBANGS:
        return "shell"
    if interpreter in PYTHON_SHEBANGS:
        return "python"
    return None


def check_shell(path: Path) -> str | None:
    result = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True
    )
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout).strip() or "bash -n failed"


def check_python(path: Path) -> str | None:
    # doraise so the error is reported here rather than printed and swallowed;
    # a throwaway cfile keeps __pycache__ out of the working tree.
    with tempfile.TemporaryDirectory() as tmp:
        cfile = Path(tmp) / "out.pyc"
        try:
            py_compile.compile(str(path), cfile=str(cfile), doraise=True)
        except py_compile.PyCompileError as exc:
            return str(exc).strip()
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=".",
        help="repository root to check (default: current directory)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the scripts that would be checked and exit",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    scripts: list[tuple[Path, str]] = []
    for path in tracked_files(root):
        if not path.is_file():
            continue
        kind = classify(path)
        if kind is not None:
            scripts.append((path, kind))
    scripts.sort()

    if args.list:
        for path, kind in scripts:
            print(f"{kind}\t{path.relative_to(root)}")
        return 0

    if not scripts:
        print("no scripts found to check -- is this a git checkout?", file=sys.stderr)
        return 2

    failures = 0
    for path, kind in scripts:
        error = check_shell(path) if kind == "shell" else check_python(path)
        if error:
            failures += 1
            print(f"{path.relative_to(root)}: {error}", file=sys.stderr)

    if failures:
        print(f"{failures} script(s) failed the syntax gate", file=sys.stderr)
        return 1

    print(f"syntax gate: {len(scripts)} script(s) checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
