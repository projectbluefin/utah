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
import gzip
import io
import json
import re
import sys
import tarfile
import tomllib
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# Hummingbird serves plain HTTP repodata. The factory publishes to a registry
# and nothing else -- the Pages mirror is gone, because linux-firmware put two
# files over the 100 MB per-file limit and the site over its 1 GB total, and a
# mirror that quietly drops packages while this script treats it as
# authoritative is worse than no mirror. So the factory is read out of the OCI
# image Utah's Containerfile already consumes, which also makes this check ask
# the same source the build does rather than a copy of it.
REPOS = {
    "public-hummingbird": "https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/",
}
FACTORY_IMAGE = "ghcr.io/projectbluefin/utah-packages"
FACTORY_TAG = "latest"


def section(path: Path, name: str) -> list[str]:
    data = tomllib.loads(path.read_text())
    return list(data.get(name, {}).get("packages", []))


def fetch(url: str, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def primary_names(stream) -> set[str]:
    """Package names out of a primary.xml stream.

    Streamed rather than parsed into a tree: Hummingbird's index is ~16 MiB
    across 66k packages and only <name> at package scope matters here.
    """
    names = set()
    for line in io.TextIOWrapper(stream, encoding="utf-8", errors="replace"):
        match = re.match(r"\s*<name>([^<]+)</name>", line)
        if match:
            names.add(match.group(1))
    return names


def registry_token(image: str) -> str:
    """ghcr hands out a pull token for public images without credentials."""
    repository = image.split("/", 1)[1]
    url = (
        "https://ghcr.io/token?service=ghcr.io"
        f"&scope=repository:{repository}:pull"
    )
    return json.loads(fetch(url))["token"]


def factory_package_names(image: str, tag: str) -> set[str]:
    """Read repodata out of the published OCI repository image.

    The factory copies repository/repodata into its own layer ahead of the full
    tree, so the metadata is a few megabytes at the front rather than behind a
    gigabyte of RPMs. Layers are tried smallest first and streamed, stopping at
    the primary index: an image built as a single layer still works, it just
    reads until it reaches repodata rather than downloading a known-small blob.
    """
    host, repository = image.split("/", 1)
    token = registry_token(image)
    accept = ", ".join(
        (
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        )
    )
    bearer = {"Authorization": f"Bearer {token}"}
    base = f"https://{host}/v2/{repository}"
    manifest = json.loads(fetch(f"{base}/manifests/{tag}", {**bearer, "Accept": accept}))
    for layer in sorted(manifest["layers"], key=lambda entry: entry["size"]):
        request = urllib.request.Request(
            f"{base}/blobs/{layer['digest']}", headers=bearer
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            media = layer.get("mediaType", "")
            if media.endswith("zstd"):
                import zstandard

                stream = zstandard.ZstdDecompressor(
                    max_window_size=2**31
                ).stream_reader(response)
            else:
                stream = gzip.GzipFile(fileobj=response)
            # Streaming mode: members arrive in archive order and are read
            # once, so nothing buffers the layer. A 500 MiB blob read whole
            # raised IncompleteRead here before.
            with tarfile.open(fileobj=stream, mode="r|*") as archive:
                for member in archive:
                    name = member.name
                    if "repodata/" not in name or "primary" not in name:
                        continue
                    if not name.endswith((".gz", ".zst")):
                        continue
                    raw = archive.extractfile(member).read()
                    if name.endswith(".zst"):
                        import zstandard

                        return primary_names(
                            zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw))
                        )
                    return primary_names(gzip.GzipFile(fileobj=io.BytesIO(raw)))
    raise SystemExit(f"ERROR: no repodata layer in {image}:{tag}")


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

    return primary_names(stream)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("overlay", type=Path, nargs="?", default=None)
    args = parser.parse_args()
    overlay = args.overlay or args.manifest.with_name("utah.toml")

    unavailable = set(section(overlay, "unavailable"))
    # Every section that reaches the image, named explicitly. This list has
    # drifted from install-packages.py's twice: [services] was never checked,
    # and [firmware] was installed but unverified the moment it was added. A
    # section missing here is not a loud failure -- it is a package the image
    # installs that nothing preflights, which is the exact shape of the bug
    # that left Utah with no firmware at all. Adding a section to utah.toml
    # means adding it in both places.
    wanted = sorted(
        set(section(args.manifest, "fedora"))
        | set(section(overlay, "gnome"))
        | set(section(overlay, "firmware"))
        | set(section(overlay, "services"))
        | set(section(overlay, "build"))
    )
    names: set[str] = set()
    for label, base in REPOS.items():
        found = repo_package_names(base)
        print(f"{label}: {len(found)} binary packages")
        names |= found
    factory = factory_package_names(FACTORY_IMAGE, FACTORY_TAG)
    print(f"{FACTORY_IMAGE}:{FACTORY_TAG}: {len(factory)} binary packages")
    names |= factory

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
