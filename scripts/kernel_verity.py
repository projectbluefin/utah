#!/usr/bin/env python3
"""Does an image's kernel read btrfs fs-verity data correctly? Pick the bootc backend.

Since 7.2, btrfs reads data through large folios and fs-verity verifies blocks
that have not arrived yet: intact files log "FILE CORRUPTED" and sometimes read
back as EIO. A composefs root is nothing but fs-verity objects, so bootc's
composefs backend is only safe on a kernel with the fix, mainline
254e15591ee4 ("btrfs: fix the fsverity callback where unexpected range is
verified", first in v7.4-rc2; docs/skills/kernel-cache.md). Every Utah kernel
qualifies today: main and nvidia boot Utah's rebuild of Fedora's kernel with
the patch (release marker `.utah1`), and the gaming flavors' OGC 7.1.x
predates large data folios (in 7.1 they exist only under
CONFIG_BTRFS_EXPERIMENTAL, which its config leaves off). Fedora's own signed
kernel (#1379) does not have the fix yet, so moving to it must move the
installers back to ostree, not break composefs installs.

A kernel counts as fixed when its release carries Utah's patch marker
(`.utahN`), its package changelog names the fix (254e15591ee4, the linux-next
id dcf8811f63af, or the subject), its version is 7.4 or newer (7.4-rc1
excepted), or it is older than 7.2 without CONFIG_BTRFS_EXPERIMENTAL.

Runs inside the image or on a booted system (it reads / unless --root):

    kernel_verity.py select --backend auto|composefs|ostree [--root DIR]

prints the decision as JSON: {"requested", "backend", "kernel", "fixed",
"reason"}. auto picks composefs on a fixed kernel and ostree otherwise;
composefs on an unfixed kernel exits 3, so a composefs medium or install is
never built on one.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

FIX_MARKERS = ("254e15591ee4", "dcf8811f63af", "fix the fsverity callback where unexpected range is verified")
UTAH_MARKER = re.compile(r"\.utah\d+(\.|$)")
RC1 = re.compile(r"(^|[.-])rc1([.-]|$)")
EXIT_UNFIXED = 3


class Unfixed(Exception):
    pass


def decide(release, changelog="", config=""):
    """(fixed, reason) for a kernel release, its package changelog and its config."""
    if UTAH_MARKER.search(release):
        return True, f"{release} is Utah's build with the btrfs fs-verity fix (.utah marker)"
    found = next((m for m in FIX_MARKERS if m in changelog), None)
    if found:
        return True, f"the {release} package changelog names the fix ({found})"
    version = re.match(r"(\d+)\.(\d+)", release)
    if not version:
        return False, f"cannot read a version from kernel release {release!r}"
    major_minor = (int(version.group(1)), int(version.group(2)))
    if major_minor >= (7, 4):
        if major_minor == (7, 4) and RC1.search(release.split("-", 1)[-1]):
            return False, f"{release} is 7.4-rc1; the fix landed in v7.4-rc2"
        return True, f"{release} is 7.4 or newer, which carries 254e15591ee4"
    if major_minor < (7, 2):
        if re.search(r"^CONFIG_BTRFS_EXPERIMENTAL=y$", config, re.M):
            return False, f"{release} predates 7.2 but enables CONFIG_BTRFS_EXPERIMENTAL (large data folios)"
        return True, f"{release} predates btrfs large data folios (7.2), so it has no regression to fix"
    return False, f"{release} is 7.2 or 7.3 without 254e15591ee4 in its changelog"


def select(requested, release, fixed, reason):
    """The decision record; Unfixed when composefs is requested on an unfixed kernel."""
    if requested not in ("auto", "composefs", "ostree"):
        raise ValueError(f"backend must be auto, composefs or ostree, not {requested!r}")
    if requested == "composefs" and not fixed:
        raise Unfixed(f"composefs requested on kernel {release}, which lacks the btrfs fs-verity fix: {reason}")
    backend = requested if requested != "auto" else ("composefs" if fixed else "ostree")
    return {"requested": requested, "backend": backend, "kernel": release, "fixed": fixed, "reason": reason}


def booted_kernel(root):
    """The one release under usr/lib/modules with a vmlinuz: the kernel bootc deploys."""
    releases = sorted(p.parent.name for p in (root / "usr/lib/modules").glob("*/vmlinuz"))
    if len(releases) != 1:
        raise ValueError(f"expected one kernel with a vmlinuz in {root}/usr/lib/modules, found {releases}")
    return releases[0]


def kernel_facts(root):
    """(release, changelog, config) of the image's kernel."""
    release = booted_kernel(root)
    vmlinuz = root / "usr/lib/modules" / release / "vmlinuz"
    # Not owned by a package (the OGC kernel), or no rpm at all, means no
    # changelog, not a failure.
    try:
        changelog = subprocess.run(["rpm", "--root", str(root), "-qf", "--changelog",
                                    f"/{vmlinuz.relative_to(root)}"], capture_output=True, text=True).stdout
    except OSError:
        changelog = ""
    config = ""
    ogc = root / "usr/lib/utah/ogc-kernel-release"
    candidates = [root / "usr/lib/modules" / release / "config", root / "boot" / f"config-{release}"]
    if ogc.is_file() and ogc.read_text().strip() == release:
        candidates.append(root / "usr/lib/utah/ogc-kernel.config")
    for path in candidates:
        if path.is_file():
            config = path.read_text(errors="replace")
            break
    return release, changelog, config


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sel = sub.add_parser("select", help="print the backend decision for the kernel under --root")
    sel.add_argument("--backend", default="auto", choices=("auto", "composefs", "ostree"))
    sel.add_argument("--root", default="/")
    args = ap.parse_args(argv)
    release, changelog, config = kernel_facts(Path(args.root))
    fixed, reason = decide(release, changelog, config)
    try:
        decision = select(args.backend, release, fixed, reason)
    except Unfixed as e:
        print(f"kernel_verity.py: {e}", file=sys.stderr)
        return EXIT_UNFIXED
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
