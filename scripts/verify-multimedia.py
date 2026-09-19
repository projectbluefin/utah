#!/usr/bin/env python3
"""Assert Utah's factory multimedia overrides: identity and codec capability.

Bluefin replaces Fedora's mesa/libva builds with negativo17's by enabling
fedora-multimedia. Utah does not enable that repository, so it takes the same
names from the utah-packages factory instead. install-packages.py adds
[multimedia_overrides] to the install transaction and versionlocks every
override it installs; this verifier asserts the builds the image actually
carries are the factory's, and probes the VA-API codec path.

Source identity is read from the RPM release tag. The factory's Hummingbird
builds carry a `hum` disttag (release `*.hum1.bfin`); negativo17's and Fedora's
builds of the same names do not. Exact NEVRAs move with the factory on every
rebuild, so the assertion checks the factory marker rather than a pinned
version, which would fail the moment the factory rebuilds.
"""

from __future__ import annotations

import argparse
import glob
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

# The factory release marker. A multimedia override whose installed release
# does not carry `hum<N>.bfin` came from somewhere other than the factory.
# A bare `hum` substring is too weak: the base/public-hummingbird repo ships
# Hummingbird-disttag builds of the same names, so `hum` alone only proves
# "built for Hummingbird", not "from the factory". The `.bfin` disttag is the
# Bluefin-factory marker, so require the full `hum<N>.bfin` release form.
FACTORY_RELEASE_RE = re.compile(r"hum\d+\.bfin\b")


def section(path: Path, name: str) -> list[str]:
    data = tomllib.loads(path.read_text())
    return list(data.get(name, {}).get("packages", []))


def installed(pkg: str) -> bool:
    return subprocess.run(
        ["rpm", "-q", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def released(pkg: str) -> str:
    out = subprocess.run(
        ["rpm", "-q", "--qf", "%{RELEASE}", pkg],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def vaapi_probe(
    which=shutil.which,
    globber=glob.glob,
    runner=subprocess.run,
    out=print,
) -> list[str]:
    """Assert the VA-API codec path, and return any failures.

    Two different things are being checked here.

    The libva shared library is hardware-independent -- either the override
    installed it or it did not -- so it always gates the build.

    Running vainfo is not. vainfo is NOT absent from the image: libva-utils
    ships /usr/bin/vainfo, it is part of the installed Bluefin contract
    (packages/bluefin.toml, [fedora]), and the factory publishes
    libva-utils-2.24.0-2.hum1.bfin. But initialising a VA-API driver needs a
    DRM render node, and an image build has no /dev/dri, so there vainfo always
    exits non-zero no matter how correct the RPM transaction was. Gate on the
    render node rather than on vainfo's exit status: skip with a note when
    there is no device to probe, and keep the probe fatal where there is one,
    so a genuine driver regression still fails the check instead of being
    quietly downgraded to a comment.
    """
    failures: list[str] = []
    if not (globber("/usr/lib64/libva.so*") or globber("/usr/lib/libva.so*")):
        failures.append("libva shared library not present")

    if not which("vainfo"):
        out("NOTE: vainfo not installed; VA-API capability not probed")
    elif not globber("/dev/dri/renderD*"):
        out(
            "NOTE: no DRM render node (/dev/dri/renderD*), as in an image "
            "build; skipping the VA-API probe rather than failing on it"
        )
    else:
        result = runner(["vainfo"], capture_output=True, text=True, check=False)
        probe = (result.stdout + result.stderr).lower()
        # Gate on the exit code plus an affirmative "no driver" signal. A bare
        # "error" substring is too noisy: libva logs non-fatal `libva error:` and
        # `error: can't connect to X server!` lines for a failed backend before a
        # fallback driver initialises with exit 0, which would false-fail a
        # working driver on real hardware (the render-node-present case only).
        if result.returncode != 0 or "no driver" in probe:
            failures.append("vainfo could not initialise a VA-API driver")
        else:
            out("vainfo initialised a VA-API driver")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true",
        help="validate the manifest only; assert nothing about installation",
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("overlay", type=Path, nargs="?", default=None)
    args = parser.parse_args()
    overlay = args.overlay or args.manifest.with_name("utah.toml")

    overrides = section(args.manifest, "multimedia_overrides")
    unavailable = set(section(overlay, "unavailable"))

    if args.check:
        assert overrides, "multimedia_overrides section is empty"
        dupes = sorted(p for p in overrides if overrides.count(p) > 1)
        assert not dupes, f"multimedia_overrides contains duplicate names: {dupes}"
        available = sorted(p for p in overrides if p not in unavailable)
        pending = sorted(p for p in overrides if p in unavailable)
        print(
            f"multimedia_overrides: {len(available)} installable from the factory,"
            f" {len(pending)} pending factory publication"
        )
        return 0

    failures: list[str] = []
    for pkg in overrides:
        if pkg in unavailable:
            continue
        if not installed(pkg):
            failures.append(f"{pkg}: not installed")
            continue
        release = released(pkg)
        if not FACTORY_RELEASE_RE.search(release):
            failures.append(
                f"{pkg}: release {release!r} is not a factory (Hummingbird 'hum<N>.bfin') build"
            )

    failures += vaapi_probe()

    if failures:
        print("multimedia contract failed:", *failures, sep="\n  ", file=sys.stderr)
        return 1
    print("multimedia contract passed: all installed overrides are factory builds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
