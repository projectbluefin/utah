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
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("overlay", type=Path, nargs="?", default=None)
    parser.add_argument("--engine", default="podman")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    overlay = args.overlay or args.manifest.with_name("utah.toml")
    base, packages = pinned_inputs(root / "Containerfile")
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
