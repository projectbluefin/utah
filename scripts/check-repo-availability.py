#!/usr/bin/env python3
"""Check the package contract against the repositories Utah actually installs from.

Every package Utah claims parity on is looked up in the repodata of the same
repositories the image build enables: Hummingbird's own overlay and the
utah-packages factory. A package that is neither available nor listed under
[unavailable] in packages/utah.toml fails here, in the seconds-long preflight
job, instead of twenty minutes into a container build.

Checking Rawhide instead would be actively misleading: Rawhide has moved to
OpenSSL 4 while the Hummingbird base pins 3.5.6, so a package being present
in Rawhide says nothing about whether Utah can install it.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import tomllib
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# The same repositories the image installs from, in the same precedence order:
# Hummingbird's own overlay plus the utah-packages factory (its Pages mirror,
# since check-repos runs in CI before the OCI repo is copied into the image).
REPOS = {
    "public-hummingbird": "https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
    "utah-packages": "https://projectbluefin.github.io/utah-packages/",
}


def section(path: Path, name: str) -> list[str]:
    data = tomllib.loads(path.read_text())
    return list(data.get(name, {}).get("packages", []))


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def repo_package_names(base: str) -> set[str]:
    repomd = ET.fromstring(fetch(base + "repodata/repomd.xml"))
    ns = {"repo": "http://linux.duke.edu/metadata/repo"}
    href = next(
        location.get("href")
        for data in repomd.findall("repo:data", ns)
        if data.get("type") == "primary"
        for location in data.findall("repo:location", ns)
    )
    raw = fetch(base + href)
    if href.endswith(".zst"):
        try:
            import zstandard
        except ModuleNotFoundError:
            print(
                "ERROR: python3-zstandard is required to read Rawhide repodata",
                file=sys.stderr,
            )
            raise SystemExit(2)
        stream = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw))
    else:
        import gzip

        stream = gzip.GzipFile(fileobj=io.BytesIO(raw))

    names = set()
    # Stream the ~16 MiB of metadata rather than holding a parse tree for
    # 66k packages; only <name> at package scope matters here.
    for line in io.TextIOWrapper(stream, encoding="utf-8", errors="replace"):
        match = re.match(r"\s*<name>([^<]+)</name>", line)
        if match:
            names.add(match.group(1))
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("overlay", type=Path, nargs="?", default=None)
    args = parser.parse_args()
    overlay = args.overlay or args.manifest.with_name("utah.toml")

    unavailable = set(section(overlay, "unavailable"))
    wanted = sorted(
        set(section(args.manifest, "fedora"))
        | set(section(overlay, "gnome"))
        | set(section(overlay, "build"))
    )
    names: set[str] = set()
    for label, base in REPOS.items():
        found = repo_package_names(base)
        print(f"{label}: {len(found)} binary packages")
        names |= found

    missing = [pkg for pkg in wanted if pkg not in names and pkg not in unavailable]
    resolved = sorted(pkg for pkg in unavailable if pkg in names)

    for pkg in resolved:
        print(f"NOTE: {pkg} is now available and can be removed from [unavailable]")
    if missing:
        print(
            f"ERROR: {len(missing)} contract packages are in none of the configured repositories.",
            file=sys.stderr,
        )
        print(
            "Add each to [unavailable] in packages/utah.toml with a tracking "
            "issue, or fix the name:",
            file=sys.stderr,
        )
        for pkg in missing:
            print(f"  - {pkg}", file=sys.stderr)
        return 1
    print(
        f"All {len(wanted) - len(unavailable)} contract packages are available "
        f"({len(unavailable)} documented as unavailable)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
