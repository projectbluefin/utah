#!/usr/bin/env python3
"""Resolve Utah's real install transaction against its pinned OCI inputs.

A name lookup against Pages cannot verify the digest in Containerfile, library
dependencies, or packages supplied only by the base image's RPM database.
Use the same installer and repository files as the image, with --assumeno.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path


# [unavailable] entries whose exclusion is permanent policy, not a supply gap:
# they resolve on the pinned inputs today and must stay out anyway. Every
# other entry is blocked debt with a tracking issue, and the day it resolves
# is the day the entry becomes a lie. The gate fails loud in both directions:
# a blocked entry that resolves must move to the contract, and a deliberate
# exclusion that stops resolving -- or vanishes from the overlay -- has lost
# the property this set asserts.
DELIBERATELY_EXCLUDED = frozenset({
    # Bluefin classic shipping every shell was a mistake Utah does not repeat;
    # users who want fish get it from Homebrew.
    "fish",
    # NOTE: firefox is not here. Its entry reads like a deliberate exclusion
    # -- Utah ships org.mozilla.firefox as a Flatpak -- but no enabled
    # repository provides the RPM at all, which makes it blocked debt with a
    # tracking issue. If the factory ever builds it, the gate fires and a
    # human decides: contract addition or deliberate exclusion.
})


def pinned_inputs(containerfile: Path) -> tuple[str, str]:
    args = dict(re.findall(r"^ARG ([A-Z_]+)=(\S+)$", containerfile.read_text(), re.M))
    base = args["BASE_IMAGE"]
    packages = f"{args['PACKAGE_IMAGE']}@{args['PACKAGE_IMAGE_SHA']}"
    for image in (base, packages):
        if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
            raise ValueError(f"Expected a digest-pinned image, got {image!r}")
    return base, packages


def verified_bytes(raw: bytes, digest: str) -> bytes:
    if digest != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise ValueError(f"Registry content does not match {digest}")
    return raw


def repository_metadata(image: str, destination: Path) -> None:
    registry, reference = image.split("/", 1)
    if registry != "ghcr.io":
        raise ValueError("The factory metadata reader currently supports ghcr.io images")
    repository, digest = reference.split("@", 1)
    query = urllib.parse.urlencode({"service": registry, "scope": f"repository:{repository}:pull"})
    with urllib.request.urlopen(f"https://{registry}/token?{query}", timeout=120) as response:
        token = json.load(response)["token"]

    def fetch(kind: str, digest: str) -> bytes:
        request = urllib.request.Request(
            f"https://{registry}/v2/{repository}/{kind}/{digest}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.oci.image.manifest.v1+json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return verified_bytes(response.read(), digest)

    manifest = json.loads(fetch("manifests", digest))
    # The factory publishes repodata first, separately from the large RPM
    # payload. Do not silently fall back to Pages or another image tag.
    layer = manifest["layers"][0]
    if layer["size"] > 64 * 1024 * 1024:
        raise ValueError("Package image lacks a small leading metadata layer; republish with repodata first")
    unpack_metadata(fetch("blobs", layer["digest"]), destination)


def unpack_metadata(raw: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for entry in archive:
            path = Path(entry.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"Unsafe metadata path: {entry.name}")
            if entry.isdir():
                continue
            if not entry.isfile() or path.parts[:2] != ("repository", "repodata"):
                raise ValueError(f"Unexpected entry in metadata layer: {entry.name}")
            target = destination / Path(*path.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(entry).read())
    if not (destination / "repodata/repomd.xml").is_file():
        raise ValueError("Pinned metadata layer contains no repomd.xml")


def unavailable_names(overlay: Path) -> list[str]:
    """The [unavailable] package list from the overlay under test."""
    return list(tomllib.loads(overlay.read_text()).get("unavailable", {}).get("packages", []))


def check_unavailable(base: str, packages: str, overlay: Path, engine: str,
                      root: Path) -> int:
    """Fail when an [unavailable] entry no longer describes reality.

    Each entry is probed with a real single-name transaction on the pinned
    base and package image, in one container run. A blocked entry that now
    resolves is stale debt: promote it to the contract. A deliberate
    exclusion that stops resolving, or disappears from the overlay, has lost
    the property DELIBERATELY_EXCLUDED asserts.
    """
    names = unavailable_names(overlay)
    print(f"Checking {len(names)} unavailable entries from {overlay} on {base}", flush=True)
    missing = sorted(DELIBERATELY_EXCLUDED - set(names))
    verdicts: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="utah-repodata-") as tmp:
        repository_metadata(packages, Path(tmp))
        result = subprocess.run([
            engine, "run", "--rm", "--platform", "linux/amd64",
            "-v", f"{tmp}:/etc/utah-packages:ro,Z",
            "-v", f"{root / 'packages'}:/etc/yum.repos.d:ro,Z",
            "-v", f"{root / 'scripts/install-packages.py'}:/tmp/install-packages.py:ro,Z",
            base, "bash", "-c",
            # The probe prints its own UTAH_RESOLVE_ONE marker; a crash that
            # produces no marker fails closed as a missing verdict below.
            'for name in "$@"; do '
            'python3 /tmp/install-packages.py --resolve-one "$name"; done',
            "utah-resolve-one", *names,
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    # The per-probe dnf output stays in the log, as --resolve-one promises:
    # each probe prints a handful of lines, and a stale verdict without the
    # transaction that produced it is not debuggable.
    print(result.stdout, end="", flush=True)
    if result.returncode == 125:
        # The engine refused to run at all: the Justfile retries this and
        # nothing else, exactly as for the full-transaction gate.
        return 125
    for name, verdict in re.findall(r"^UTAH_RESOLVE_ONE (\S+) (\d+)$", result.stdout, re.M):
        verdicts[name] = int(verdict)
    failures = []
    for name in names:
        if name not in verdicts:
            failures.append(f"{name}: no probe verdict in the container output; refusing to pass blind")
        elif name in DELIBERATELY_EXCLUDED:
            if verdicts[name] != 0:
                failures.append(f"{name}: deliberately excluded but no longer resolves; "
                                "move it to blocked debt with a tracking issue")
            else:
                print(f"{name}: deliberately excluded, still resolves (stays out)")
        elif verdicts[name] == 0:
            failures.append(f"{name}: now resolves on the pinned inputs; "
                            "move it out of [unavailable] into the contract")
        else:
            print(f"{name}: still unavailable")
    for name in missing:
        failures.append(f"{name}: deliberate exclusion missing from [unavailable]; "
                        "policy was dropped, not repealed")
    if failures:
        print(f"ERROR: {len(failures)} unavailable entries no longer describe reality:",
              file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"All {len(names)} unavailable entries still describe reality.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("overlay", type=Path, nargs="?", default=None)
    parser.add_argument("--engine", default="podman")
    parser.add_argument("--check-unavailable", action="store_true",
                        help="probe [unavailable] entries instead of resolving the transaction")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    overlay = args.overlay or args.manifest.with_name("utah.toml")
    base, packages = pinned_inputs(root / "Containerfile")
    if args.check_unavailable:
        return check_unavailable(base, packages, overlay, args.engine, root)
    print(f"Checking package repository {packages} on {base}", flush=True)
    with tempfile.TemporaryDirectory(prefix="utah-repodata-") as tmp:
        repository_metadata(packages, Path(tmp))
        return subprocess.run([
            args.engine, "run", "--rm", "--platform", "linux/amd64",
            "-v", f"{tmp}:/etc/utah-packages:ro,Z",
            "-v", f"{root / 'packages'}:/etc/yum.repos.d:ro,Z",
            "-v", f"{args.manifest.resolve()}:/tmp/bluefin.toml:ro,Z",
            "-v", f"{overlay.resolve()}:/tmp/utah.toml:ro,Z",
            "-v", f"{root / 'scripts/install-packages.py'}:/tmp/install-packages.py:ro,Z",
            base, "python3", "/tmp/install-packages.py", "--resolve",
            "/tmp/bluefin.toml", "/tmp/utah.toml",
        ], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
