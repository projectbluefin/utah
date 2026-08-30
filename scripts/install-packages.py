#!/usr/bin/env python3
"""Install Utah's package contract: Bluefin's manifest plus the Rawhide overlay.

Mirrors projectbluefin/bluefin's build_files/base/03-packages.sh and
build_files/shared/package-lib.sh, adapted to a single Fedora Rawhide repo.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

# Utah installs only from its Hummingbird base plus the utah-packages
# factory, which publishes every GNOME 51 and Bluefin-parity binary this
# image needs rebuilt against Hummingbird. Fedora repositories are never
# enabled at runtime: they are bootstrap material for the package factory's
# buildroot, not a source of installed packages.
# The factory is first so its Hummingbird-targeted rebuilds win over an
# equally-versioned Hummingbird package. The repository is copied from the
# digest-pinned OCI package image by Containerfile.
REPOS = ("utah-packages", "public-hummingbird-x86_64-rpms")


def section(path: Path, name: str) -> list[str]:
    data = tomllib.loads(path.read_text())
    return list(data.get(name, {}).get("packages", []))


def fedora_major() -> str:
    """Read %fedora the way Bluefin's build scripts do."""
    out = subprocess.run(
        ["rpm", "-E", "%fedora"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def contract(base: Path, overlay: Path, major: str | None) -> list[str]:
    """The exact set of packages the built image must contain.

    Bluefin installs [fedora] plus the [fedora_v<major>] section for the
    Fedora release it targets, and simply skips that section when it does not
    exist.  Rawhide is Fedora 46 and upstream's manifest stops at v44, so on
    Rawhide this resolves to [fedora] alone -- which is what Bluefin itself
    would install there.  Keeping the lookup dynamic means Utah picks the
    section up for free the moment upstream adds one.
    """
    packages = section(base, "fedora")
    if major:
        packages += section(base, f"fedora_v{major}")
    packages += section(overlay, "gnome")
    # Service packages are part of the desktop contract as well: 40-services.sh
    # cannot enable what the server base never installed.
    packages += section(overlay, "services")
    unavailable = set(section(overlay, "unavailable"))
    # Deduplicate while preserving order so build logs stay diffable.
    seen: dict[str, None] = {}
    for pkg in packages:
        if pkg not in unavailable:
            seen.setdefault(pkg, None)
    return list(seen)


def dnf_path() -> str:
    dnf = shutil.which("dnf5") or shutil.which("dnf")
    if not dnf:
        raise RuntimeError("Hummingbird base does not provide dnf or dnf5")
    return dnf


def run(*args: str) -> int:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, check=False).returncode


def installed(packages: list[str]) -> list[str]:
    if not packages:
        return []
    out = subprocess.run(
        ["rpm", "-qa", "--queryformat=%{NAME}\n", *packages],
        capture_output=True,
        text=True,
        check=False,
    )
    return sorted(set(out.stdout.split()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "overlay", type=Path, nargs="?", default=None,
        help="defaults to utah.toml alongside the Bluefin manifest",
    )
    args = parser.parse_args()
    overlay = args.overlay or args.manifest.with_name("utah.toml")

    if args.check:
        # No rpmdb to consult off-image, so validate the manifests only.
        packages = contract(args.manifest, overlay, major=None)
        if not packages:
            raise ValueError("Bluefin package manifest is empty")
        unavailable = section(overlay, "unavailable")
        overlap = sorted(set(unavailable) & set(packages))
        if overlap:
            raise ValueError(f"[unavailable] packages still in install set: {overlap}")
        print(f"validated {len(packages)} Bluefin parity packages")
        print(f"documented as unavailable: {len(unavailable)}")
        return 0

    dnf = dnf_path()
    major = fedora_major()
    packages = contract(args.manifest, overlay, major)
    build_deps = section(overlay, "build")
    excluded = section(args.manifest, "excluded")

    # Record exactly what this run resolved, so the contract check asserts the
    # set that was actually asked for rather than recomputing it and drifting.
    # It drifted once: install added [fedora_v<major>] and the verifier never
    # did, so a contract package was installed and never checked.
    resolved = Path("/usr/share/utah/contract.txt")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text("".join(f"{pkg}\n" for pkg in packages))
    except OSError as error:
        print(f"WARNING: could not record the resolved contract: {error}")

    print(f"Fedora release is {major}", flush=True)
    for pkg in section(overlay, "unavailable"):
        # Loud, not silent: a parity gap the operator should see in the log.
        print(f"NOTE: {pkg} has no Rawhide source and is skipped (see packages/utah.toml)")

    # Bluefin excludes PackageKit from its bulk install; an image-based system
    # must not carry a second package manager that can write to /usr.
    rc = run(
        dnf, "-y", "--disablerepo=*",
        *(f"--enablerepo={r}" for r in REPOS),
        "-x", "PackageKit*", "install", *packages, *build_deps,
    )
    if rc:
        return rc

    # Everything in the contract is installed on purpose.  Without this, dnf
    # treats packages that merely arrived as dependencies as autoremovable,
    # and the [excluded] removal below drags them back out -- which is how
    # xdg-desktop-portal-gnome disappeared from an image that installed it.
    rc = run(dnf, "-y", "mark", "user", *packages, *build_deps)
    if rc:
        return rc

    # Mirror remove_excluded_packages: only remove what is actually installed,
    # and never let the removal cascade into the contract.
    present = installed(excluded)
    if present:
        print(f"Removing {len(present)} excluded packages: {' '.join(present)}")
        # dnf5 requires --no-autoremove after the subcommand, not before it:
        # "The argument is available for commands: remove. (It has to be placed
        # after the command.)" dnf4 accepts either position, so this ordering
        # works on both.
        rc = run(dnf, "-y", "remove", "--no-autoremove", *present)
        if rc:
            return rc
    else:
        print("No excluded packages found to remove.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
