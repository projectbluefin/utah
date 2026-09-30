#!/usr/bin/env python3
"""Keep the Containerfile's package-factory pin from going stale.

`ARG PACKAGE_IMAGE_SHA` in the Containerfile is the digest of
`ghcr.io/projectbluefin/utah-packages`: the RPM repository every image installs
from. It is a deliberate pin -- an image build has to be reviewable against the
exact package set it consumed -- but nothing revved it. The factory published
GNOME 51 finals under a new digest and this repository kept building against
whatever the pin said until someone noticed by hand (#336).

The fix is to make the rev a routine pull request instead of a discovery. A
scheduled workflow (`.github/workflows/bump-factory-pin.yml`) runs this script
against `testing`, and the resulting diff -- one line -- is reviewed and merged
like any other change. Nothing here merges anything, and a stale pin is still
only ever *reported* by the caller; writing the file is opt-in by way of the
absence of `--check`.

Resolution is a plain registry manifest request on the tag, reading the digest
off the `Docker-Content-Digest` response header, through the anonymous bearer
token GHCR hands out for a public image, so there is no skopeo install and no
credential in the log. `--image` and `--tag` exist so the tests and a future
pin on a different factory tag do not have to edit this file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTAINERFILE = ROOT / "Containerfile"

DEFAULT_IMAGE = "ghcr.io/projectbluefin/utah-packages"
DEFAULT_TAG = "latest"

DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
IMAGE_ARG_RE = re.compile(r"^ARG PACKAGE_IMAGE=(?P<image>\S+)\s*$", re.MULTILINE)
SHA_ARG_RE = re.compile(
    r"^ARG PACKAGE_IMAGE_SHA=(?P<digest>\S+)\s*$", re.MULTILINE)
REF_ARG_RE = re.compile(
    r"^ARG PACKAGE_IMAGE_REF=(?P<ref>\S+)\s*$", re.MULTILINE)

MANIFEST_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


class PinError(Exception):
    """The Containerfile does not say what this script expects it to say."""


def split_image(image: str) -> tuple[str, str]:
    """(registry, repository-without-registry) for a ghcr-style reference."""
    parts = image.split("/", 1)
    if len(parts) != 2 or "." not in parts[0] and ":" not in parts[0]:
        raise PinError(
            f"image {image!r} carries no registry host; expected "
            "'ghcr.io/<owner>/<name>'")
    return parts[0], parts[1]


def _request(url: str, token: str | None, accept: str | None = None) -> dict:
    headers = {"User-Agent": "utah-bump-factory-pin"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return dict(response.headers)


def anonymous_token(registry: str, repository: str) -> str:
    """The pull token a public registry hands out without credentials."""
    url = (f"https://{registry}/token?service={registry}"
           f"&scope=repository:{repository}:pull")
    request = urllib.request.Request(
        url, headers={"User-Agent": "utah-bump-factory-pin"}, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())["token"]


def resolve_digest(image: str = DEFAULT_IMAGE, tag: str = DEFAULT_TAG) -> str:
    """The digest the registry currently serves for image:tag."""
    registry, repository = split_image(image)
    token = anonymous_token(registry, repository)
    headers = _request(f"https://{registry}/v2/{repository}/manifests/{tag}",
                       token, MANIFEST_ACCEPT)
    digest = headers.get("Docker-Content-Digest") or headers.get(
        "docker-content-digest")
    if not digest:
        raise PinError(
            f"{registry} served no Docker-Content-Digest for {image}:{tag}; "
            "refusing to guess a pin")
    if not DIGEST_RE.match(digest.strip()):
        raise PinError(f"registry returned a malformed digest: {digest!r}")
    return digest.strip()


def current_pin(text: str) -> str:
    """The digest the Containerfile pins, or PinError if it is unusable."""
    match = SHA_ARG_RE.search(text)
    if not match:
        raise PinError("Containerfile has no 'ARG PACKAGE_IMAGE_SHA=' line")
    digest = match.group("digest")
    if not DIGEST_RE.match(digest):
        raise PinError(
            f"ARG PACKAGE_IMAGE_SHA={digest} is not a sha256 digest; this "
            "script will not rewrite a pin it does not understand")
    return digest


def verify_shape(text: str) -> None:
    """The three ARG lines have to compose, or a rewrite is not enough."""
    image = IMAGE_ARG_RE.search(text)
    ref = REF_ARG_RE.search(text)
    if not image:
        raise PinError("Containerfile has no 'ARG PACKAGE_IMAGE=' line")
    if not ref:
        raise PinError("Containerfile has no 'ARG PACKAGE_IMAGE_REF=' line")
    expected = "${PACKAGE_IMAGE}@${PACKAGE_IMAGE_SHA}"
    if ref.group("ref") != expected:
        raise PinError(
            f"ARG PACKAGE_IMAGE_REF={ref.group('ref')} is not {expected}; a "
            "digest bump would not reach the build")


def rewrite(text: str, digest: str) -> str:
    """The same Containerfile with the factory pin moved to digest."""
    if not DIGEST_RE.match(digest):
        raise PinError(f"refusing to write a malformed digest: {digest!r}")
    current_pin(text)  # refuse to rewrite a line we could not have parsed
    return SHA_ARG_RE.sub(f"ARG PACKAGE_IMAGE_SHA={digest}", text, count=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report staleness and exit non-zero; write nothing")
    parser.add_argument("--print", dest="print_digest", action="store_true",
                        help="print the resolved digest and write nothing")
    parser.add_argument("--digest",
                        help="write this digest instead of resolving one; the "
                             "resolve and propose jobs of the same run then "
                             "cannot disagree about which digest they mean")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--containerfile", type=Path, default=CONTAINERFILE)
    args = parser.parse_args(argv)

    try:
        text = args.containerfile.read_text()
        verify_shape(text)
        pinned = current_pin(text)
        latest = args.digest or resolve_digest(args.image, args.tag)
    except (PinError, OSError, urllib.error.URLError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not DIGEST_RE.match(latest):
        print(f"error: {latest!r} is not a sha256 digest", file=sys.stderr)
        return 1

    if args.print_digest:
        print(latest)
        return 0

    if pinned == latest:
        print(f"up to date: {args.image}:{args.tag} is {pinned}")
        return 0

    if args.check:
        print(f"stale: {args.image}:{args.tag} is {latest}, "
              f"{args.containerfile.name} pins {pinned}",
              file=sys.stderr)
        return 1

    args.containerfile.write_text(rewrite(text, latest))
    print(f"bumped: {pinned} -> {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
