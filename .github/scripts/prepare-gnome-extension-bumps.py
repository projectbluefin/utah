#!/usr/bin/env python3
"""Advance valid GNOME extension pins and report unsafe upstream heads."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHELL_VERSION_HELPER = REPO_ROOT / "scripts/gnome-extension-shell-versions.py"


@dataclass(frozen=True)
class Bump:
    path: str
    pinned: str
    upstream: str
    shell_versions: str
    result: str


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result


def shell_versions(root: Path, path: str) -> tuple[bool, str, str]:
    result = subprocess.run(
        [sys.executable, str(SHELL_VERSION_HELPER), str(root / path)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


def submodules(root: Path) -> list[tuple[str, str]]:
    result = run_git(
        root,
        "config",
        "--file",
        ".gitmodules",
        "--get-regexp",
        r"^submodule\..*\.path$",
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"cannot read submodule paths: {result.stderr.strip()}")
    entries = []
    for line in result.stdout.splitlines():
        key, path = line.split(maxsplit=1)
        name = key[len("submodule.") : -len(".path")]
        entries.append((name, path))
    return entries


def tracked_branch(root: Path, name: str) -> str | None:
    result = run_git(
        root, "config", "--file", ".gitmodules", "--get", f"submodule.{name}.branch", check=False
    )
    if result.returncode == 1:
        return None
    if result.returncode:
        raise RuntimeError(f"cannot read branch for submodule {name}: {result.stderr.strip()}")
    return result.stdout.strip()


def update_submodule(root: Path, name: str, path: str) -> tuple[str, str]:
    pinned = run_git(root, "rev-parse", f"HEAD:{path}").stdout.strip()
    run_git(root, "submodule", "update", "--init", "--depth=1", "--", path)

    branch = tracked_branch(root, name)
    if branch:
        run_git(
            root,
            "-C",
            path,
            "fetch",
            "--depth=1",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        )
        run_git(root, "-C", path, "checkout", "--detach", f"refs/remotes/origin/{branch}")
    else:
        run_git(root, "submodule", "update", "--remote", "--depth=1", "--", path)

    return pinned, run_git(root, "-C", path, "rev-parse", "HEAD").stdout.strip()


def prepare(root: Path, report_path: Path) -> list[Bump]:
    bumps = []
    for name, path in submodules(root):
        pinned, upstream = update_submodule(root, name, path)
        if pinned == upstream:
            bumps.append(Bump(path, pinned, upstream, "—", "unchanged"))
            continue

        valid, versions, _ = shell_versions(root, path)
        if valid:
            bumps.append(Bump(path, pinned, upstream, versions, "proposed"))
            continue

        # Keep the last known-good gitlink and allow unrelated valid updates
        # through. The rejected upstream SHA remains visible in the report.
        run_git(root, "-C", path, "checkout", "--detach", pinned)
        pinned_valid, pinned_versions, detail = shell_versions(root, path)
        if not pinned_valid:
            raise RuntimeError(
                f"pinned extension metadata is invalid for {path}: {detail}; refusing update"
            )
        print(
            f"::warning title=GNOME extension bump skipped::{path} at {upstream} has missing or invalid metadata; keeping {pinned}",
            file=sys.stderr,
        )
        bumps.append(
            Bump(path, pinned, upstream, pinned_versions, "skipped (invalid upstream metadata)")
        )

    report = "\n".join(
        [
            "# GNOME extension bumps",
            "",
            "Valid tracked-branch heads are proposed; missing or invalid metadata keeps the existing pin and does not block other extensions.",
            "This workflow dispatches contract-only CI on the proposed branch. It does not build an image or run the image-side extension verifier.",
            "No automated merge is enabled; upstream changes can still break GNOME 51 or the image build assumptions.",
            "",
            "## Heads",
            "",
            "| extension | pinned | upstream head | GNOME shell-version | result |",
            "|---|---|---|---|---|",
            *[
                f"| `{bump.path}` | `{bump.pinned[:12]}` | `{bump.upstream[:12]}` | {bump.shell_versions} | {bump.result} |"
                for bump in bumps
            ],
            "",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a") as summary:
            summary.write(report)
    return bumps


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.root.resolve(), args.report.resolve())
    except (OSError, RuntimeError) as exc:
        print(f"Cannot prepare GNOME extension bumps: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
